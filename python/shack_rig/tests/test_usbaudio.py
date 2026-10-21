"""The receive audio path on a USB cable, exercised without a radio.

Ported from OpenHamShack Icom's tools/selftest_usbaudio.py. Everything here
runs the real code: the capture runs a real subprocess on a real pipe, and the
splitter runs on real off-air audio when it is available.

**The number this file exists to protect is the sample rate**, and it is worth
saying why a test for it is not obvious. A rate that is wrong by 2.1% does not
fail. The levels are right, the capture runs, the decoder runs, and FT8
decodes - it simply decodes an eighth as much and reports every frequency 2.1%
high, in a log nobody re-reads. The Icom app's `docs/OFF-AIR.md` has the
measurement: six decodes against fifty-one on the same 2.5 minutes of 20 m.

**And the second thing it protects is the phase of the decimation.** Taking
every fourth sample is exact only if "every fourth" is counted from the start
of the stream rather than from the start of each block, and a pipe hands over
blocks of whatever length it likes. Getting that wrong is not an exception and
not silence: it is audio that sounds perfectly ordinary and decodes slightly
worse, for ever. So the splitter is fed the same audio in one piece, in
awkward pieces and one byte at a time, and all three have to produce the same
bytes.

No sound card is opened. `arecord` is replaced by a script written into a
pytest temporary directory that speaks its two halves of the interface -
answering `--dump-hw-params` on stderr and writing raw samples to stdout - so
the subprocess, the pipe, the block reads, the restart and the kill are all
real and none of it touches the host's audio hardware.

The off-air recording is not copied into this repository: it is 360 kB of the
Icom app's digi fixtures. Point SHACK_RIG_OFF_AIR at
`icom/app/digi/offair/40m-ft8-260919-161015.wav` in an Icom app checkout to
run the checks that want it; without it they skip, or fall back to noise where
the original did.
"""

from __future__ import annotations

import array
import asyncio
import math
import os
import shutil
import stat
from pathlib import Path

import pytest

from shack_rig import usbaudio
from shack_rig.usbaudio import (
    STEREO, AudioError, Card, DECODE_RATE, Format, Levels, Split, UsbAudio,
    _what, choose, find_card, level, parse_hw_params,
)

#: What `arecord -D hw:2,0 --dump-hw-params` really printed for the IC-7610's
#: codec on `shack`, copied out rather than tidied. The shape is the point: the
#: card has one capture rate, 48000, and ALSA reports the merged interval of
#: every altsetting as `RATE: [8000 48000]` - so the middle of that range is a
#: lie and only the top of it is a rate the hardware really has. A parser
#: written against a tidied-up version of this would believe the middle.
SEVEN_HW_PARAMS = """HW Params of device "hw:2,0":
--------------------
ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED
FORMAT:  S8 S16_LE
SUBFORMAT:  STD MSBITS_MAX
SAMPLE_BITS: [8 16]
FRAME_BITS: [8 32]
CHANNELS: [1 2]
RATE: [8000 48000]
PERIOD_TIME: [1000 1000000]
PERIOD_SIZE: [16 48000]
PERIOD_BYTES: [64 192000]
PERIODS: [2 1024]
BUFFER_TIME: (666 2000000]
BUFFER_SIZE: [32 96000]
BUFFER_BYTES: [64 384000]
TICK_TIME: ALL
--------------------
arecord: set_params:1387: Sample format non available
Available formats:
- S8
- S16_LE
"""

#: The real string from `ls -l /dev/snd/by-id` on `shack` with the 7610 plugged
#: in, used verbatim for the same reason the serial one is in usbbench.py:
#: following it to a card number is parsing this exact shape, and a tidied
#: version would test the tidying.
CODEC_BY_ID = "usb-Burr-Brown_from_TI_USB_Audio_CODEC-00"

#: A real 15 second slot of 40 m, kept by the Icom app's digi selftest. Three
#: signals; the app's version of this file decodes them with jt9.
OFF_AIR_NAME = "40m-ft8-260919-161015.wav"


def off_air() -> Path | None:
    p = os.environ.get("SHACK_RIG_OFF_AIR", "")
    return Path(p) if p and Path(p).is_file() else None


# -- material ---------------------------------------------------------------


def noise(n: int, seed: int = 1) -> bytes:
    """n pseudorandom 16-bit samples, the same ones every run.

    Random rather than a tone, because a smooth signal hides a slicing bug: get
    the stride wrong on a sine wave and the result is still a sine wave. The
    generator is written out rather than taken from `random` so that the bytes
    do not depend on which Python this runs under.
    """
    a = array.array("h")
    x = seed
    for _ in range(n):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        a.append((x >> 8) % 65536 - 32768)
    return a.tobytes()


def interleave(*channels: bytes) -> bytes:
    """Channels of 16-bit mono into one interleaved stream."""
    arrays = []
    for c in channels:
        a = array.array("h")
        a.frombytes(c)
        arrays.append(a)
    n = min(len(a) for a in arrays)
    out = array.array("h", bytes(2 * n * len(arrays)))
    for i, a in enumerate(arrays):
        out[i::len(arrays)] = a[:n]
    return out.tobytes()


def upsample4(pcm: bytes) -> bytes:
    """12 kHz to 48 kHz by linear interpolation - a plausible recording.

    Interpolated rather than repeated, and that is the whole reason this
    function is not two lines. Repeating each sample four times makes every
    phase of a 4:1 decimation give the same answer, so a decimator that lost
    its place between blocks would pass every check in this file. Interpolating
    makes phase 0 recover the original samples exactly and every other phase
    recover something else, which is what the checks below rest on.
    """
    a = array.array("h")
    a.frombytes(pcm)
    out = array.array("h", bytes(len(a) * 8))
    out[0::4] = a
    for k in (1, 2, 3):
        got = array.array("h", [0]) * len(a)
        for i in range(len(a) - 1):
            got[i] = a[i] + (a[i + 1] - a[i]) * k // 4
        got[-1] = a[-1]
        out[k::4] = got
    return out.tobytes()


def goertzel(pcm: bytes, freq: float, rate: int = DECODE_RATE) -> float:
    """How much of one frequency is in a block of samples.

    Here rather than a full transform because one bin is all that is wanted and
    this is twenty lines of arithmetic with no dependency - the rule about not
    adding one for forty lines of standard library applies to test code as
    much as to the rest.
    """
    a = array.array("h")
    a.frombytes(pcm)
    w = 2.0 * math.pi * freq / rate
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for s in a:
        s0 = s + coeff * s1 - s2
        s2, s1 = s1, s0
    return math.hypot(s1 - s2 * math.cos(w), s2 * math.sin(w)) / len(a)


def tone(freq: float, seconds: float, rate: int) -> bytes:
    a = array.array("h")
    for i in range(int(seconds * rate)):
        a.append(int(12000 * math.sin(2 * math.pi * freq * i / rate)))
    return a.tobytes()


def slowly(interleaved: bytes, channels: int, step: int) -> list[bytes]:
    """The obvious implementation, for the fast one to be checked against.

    Written as the loop anybody would write first, over the whole stream at
    once, so that it shares no code with `Split` - a reference that called the
    same slice would agree with a bug.
    """
    a = array.array("h")
    a.frombytes(interleaved)
    out = []
    for ch in range(channels):
        got = array.array("h")
        i = ch
        while i < len(a) - (channels - 1 - ch):
            got.append(a[i])
            i += channels * step
        out.append(got.tobytes())
    return out


# -- what the card says it can do -------------------------------------------


def test_reading_the_card():
    params = parse_hw_params(SEVEN_HW_PARAMS)
    assert params.get("RATE") == (8000, 48000), \
        f"the rate interval is read: {params.get('RATE')}"
    assert params.get("CHANNELS") == (1, 2), \
        f"and the channel interval: {params.get('CHANNELS')}"

    rate, channels = choose(params)
    # The whole of the capture decision, on the real radio's real card.
    assert rate == 48000, f"the IC-7610's codec is captured at 48 kHz: {rate}"
    assert channels == 2, f"in two channels, because that is two receivers: {channels}"
    assert rate % DECODE_RATE == 0, "and 48000 divides into 12000 exactly"

    # 12000 is inside the interval ALSA reports and is not a rate this card
    # has. Nothing may choose it on the strength of it being in the range -
    # asking hw: for it fails, and asking plughw: for it is the 2.1% bug.
    assert rate != 12000, "the middle of the reported range is not chosen"


def test_a_card_that_cannot_do_a_multiple_of_12k_is_refused_not_resampled():
    with pytest.raises(AudioError) as refused:
        choose({"RATE": (44100, 44100), "CHANNELS": (1, 2)})
    assert "2.1" in str(refused.value), str(refused.value)


def test_a_96k_card_is_captured_at_48k():
    # Capped rather than taken: 96 kHz off a radio is four times the work for
    # the codec's own noise floor.
    rate, _ = choose({"RATE": (8000, 96000), "CHANNELS": (1, 2)})
    assert rate == 48000, rate


def test_channels_are_named_receivers():
    # A mono capture interface is one receiver and says so.
    assert len(Format("hw:9,0", 48000, 1).receivers()) == 1, "one channel is one receiver"
    assert len(Format("hw:9,0", 48000, 2).receivers()) == 2, "two channels are two"
    assert [r.key for r in Format("hw:9,0", 48000, 2).receivers()] == ["left", "right"], \
        "and they are named, not numbered"


# -- which card is the radio ------------------------------------------------

HAVE = [
    Card(0, "Generic", "HDA-Intel", "HD-Audio Generic"),
    Card(1, "Generic_1", "HDA-Intel", "HD-Audio Generic"),
    Card(2, "CODEC", "USB-Audio", "USB Audio CODEC"),
]


def test_the_only_usb_card_is_chosen_over_the_motherboards():
    device, how = find_card("", found=HAVE)
    assert device == "hw:2,0", device
    assert "CODEC" in how, f"and the choice is explained: {how}"


def test_two_usb_cards_are_refused_rather_than_guessed():
    # A library that picks one and says nothing decodes the wrong radio and
    # reads as a quiet band.
    two = HAVE + [Card(3, "CODEC_1", "USB-Audio", "USB Audio CODEC")]
    with pytest.raises(AudioError) as refused:
        find_card("", found=two)
    assert "CODEC_1" in str(refused.value) and "by-id" in str(refused.value), \
        str(refused.value)


def test_no_usb_card_at_all_is_said_in_words():
    with pytest.raises(AudioError) as refused:
        find_card("", found=HAVE[:2])
    assert "Devices" in str(refused.value), str(refused.value)


def test_a_by_id_link_is_followed_to_the_card_it_points_at_now(tmp_path):
    # Followed at the moment of use, which is the point of storing the link:
    # the number moves between boots.
    snd = tmp_path / "dev-snd"
    (snd / "by-id").mkdir(parents=True)
    (snd / "controlC2").write_bytes(b"")
    link = snd / "by-id" / CODEC_BY_ID
    link.symlink_to("../controlC2")
    device, _ = find_card(str(link), by_id=str(snd / "by-id"))
    assert device == "hw:2,0", f"a by-id link becomes the card it points at now: {device}"

    # And the same link after the cards were renumbered points somewhere
    # else, which is the whole reason a number is never stored.
    link.unlink()
    (snd / "controlC1").write_bytes(b"")
    link.symlink_to("../controlC1")
    device, _ = find_card(str(link), by_id=str(snd / "by-id"))
    assert device == "hw:1,0", \
        f"and at a different card after a reboot renumbered them: {device}"


def test_an_alsa_name_is_used_as_given():
    # Including plughw. Somebody who asks for the plug layer has asked for
    # it; this is not the place to argue.
    device, _ = find_card("plughw:CARD=CODEC,DEV=0")
    assert device == "plughw:CARD=CODEC,DEV=0", device


# -- deinterleaving and decimating ------------------------------------------

LEFT = noise(4000, seed=7)
RIGHT = noise(4000, seed=99)
STEREO_PCM = interleave(LEFT, RIGHT)


def test_splitting_matches_the_obvious_implementation():
    got = Split(2, 48000).feed(STEREO_PCM)
    assert len(got) == 2, "one block gives one stream per channel"
    assert len(got[0]) == len(LEFT) // 4, \
        f"and a quarter of the samples in each: {len(got[0])} of {len(LEFT)}"
    want = slowly(STEREO_PCM, 2, 4)
    assert got[0] == want[0], "the left channel matches the obvious implementation"
    assert got[1] == want[1], "and the right one does"
    assert got[0] != got[1], "and the two are not the same thing"

    # Phase. The first kept sample of each channel is the first sample of that
    # channel, and the next is four frames later - not four samples.
    a = array.array("h"); a.frombytes(got[0])
    src = array.array("h"); src.frombytes(LEFT)
    assert list(a[:5]) == [src[0], src[4], src[8], src[12], src[16]], \
        f"kept samples are every fourth frame from the start: {list(a[:5])} against {list(src[:20:4])}"


def _pieces(data: bytes, sizes) -> list[bytes]:
    out, i, n = [], 0, 0
    while i < len(data):
        take = sizes(n)
        out.append(data[i:i + take])
        i += take
        n += 1
    return out


@pytest.mark.parametrize("name,sizes", [
    ("one byte at a time", lambda n: 1),
    ("in primes", lambda n: (7, 13, 1, 31, 2, 97, 5)[n % 7]),
    ("in 3-byte pieces, which split a frame in half", lambda n: 3),
])
def test_however_it_is_fed_the_same_bytes_come_out(name, sizes):
    """The one that matters.

    A pipe hands over whatever length it feels like, and a splitter that
    starts counting again at each block silently changes the phase of the
    decimation at every boundary.
    """
    want = slowly(STEREO_PCM, 2, 4)
    s = Split(2, 48000)
    acc = [bytearray(), bytearray()]
    for piece in _pieces(STEREO_PCM, sizes):
        for ch, chunk in enumerate(s.feed(piece)):
            acc[ch] += chunk
    assert bytes(acc[0]) == want[0] and bytes(acc[1]) == want[1], \
        f"fed {name}: {len(acc[0])} against {len(want[0])}"


def test_a_mono_capture_decimates_too():
    # What a radio set to Main-only or a one-channel capture interface gives.
    assert Split(1, 48000).feed(LEFT)[0] == slowly(LEFT, 1, 4)[0]


def test_a_rate_that_does_not_divide_is_refused():
    # At construction rather than approximated, because approximating it is
    # the 2.1% bug.
    with pytest.raises(AudioError):
        Split(2, 44100)


def test_the_rate_is_exact():
    """A tone goes in at a known frequency and has to come out at it.

    The arithmetic half of the rate check, and the one that runs anywhere. A
    resampler that is 2.1% out moves a 1500 Hz tone to 1531 Hz, which is four
    bins of an FT8 decode and is the difference between fifty-one decodes and
    six - and is completely invisible to the ear, to a level meter, and to any
    check that only asks whether audio arrived.
    """
    stereo = interleave(tone(1500.0, 1.0, 48000), tone(2200.0, 1.0, 48000))
    out = Split(2, 48000).feed(stereo)

    at = {f: goertzel(out[0], f) for f in (1450, 1469, 1500, 1531, 1550)}
    best = max(at, key=at.get)
    assert best == 1500, "1500 Hz in is 1500 Hz out: " + \
        ", ".join(f"{f}:{v:.0f}" for f, v in at.items())
    # Said as a ratio as well, so that a future change which merely blurs the
    # tone rather than moving it also fails.
    assert at[1500] > 5 * at[1531], \
        f"and 2.1% high is not where it landed: {at[1500]:.0f} against {at[1531]:.0f}"
    # The other channel is the other tone and has not leaked into this one.
    assert goertzel(out[1], 2200) > 5 * goertzel(out[1], 1500), \
        "the right channel carries its own tone, not the left's"


# -- what the radio is actually sending -------------------------------------


def test_saying_what_is_on_the_cable():
    """Two channels is not the same claim as two receivers.

    A 7610 whose ACC/USB output is set to Main only still presents a
    two-channel capture interface and puts the same audio on both. Nothing over
    CI-V says which it is doing, so the samples are the only evidence, and a
    library that counted channels and offered a second decoder would be
    offering a decoder for a copy.
    """
    # Real 40 m when it is to hand, as the original used; noise otherwise,
    # which is also what the original fell back to.
    rec = off_air()
    real = rec.read_bytes()[44:] if rec else noise(120000, 3)
    other = noise(len(real) // 2, seed=42)

    same = Levels(STEREO)
    same.compare(real, real)
    assert same.same_audio() is True, \
        f"the same audio on both channels is seen as one receiver: {same.public()}"

    differ = Levels(STEREO)
    differ.compare(real, other)
    assert differ.same_audio() is False, f"different audio is seen as two: {differ.public()}"

    # And it says so in a sentence, because the number is no use on a page.
    assert "two receivers" in _what(STEREO, False) and "Main" in _what(STEREO, True), \
        _what(STEREO, True)

    # Not enough audio yet is its own answer rather than a guess at one.
    assert Levels(STEREO).same_audio() is None, "before there is enough audio it says so"

    peak, rms = level(real)
    assert 0 < rms < peak <= 32768, f"a level is measured off the audio: peak {peak}, rms {rms}"
    assert level(bytes(4000)) == (0, 0), "silence measures as silence"


# -- the capture, as a real subprocess on a real pipe ------------------------

FAKE_ARECORD = r'''#!/usr/bin/env python3
"""Enough of arecord to exercise the library: hw params, or raw samples."""
import os, sys, time

if "--dump-hw-params" in sys.argv:
    sys.stderr.write(os.environ["FAKE_PARAMS"])
    sys.exit(0)

if os.environ.get("FAKE_FAIL"):
    sys.stderr.write("arecord: main:834: audio open error: Device or resource busy\n")
    sys.exit(1)

# Record that this run happened, so a test can count restarts.
with open(os.environ["FAKE_RUNS"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")

data = open(os.environ["FAKE_PCM"], "rb").read()
step = int(os.environ.get("FAKE_CHUNK", "0")) or len(data)
out = sys.stdout.buffer
for i in range(0, len(data), step):
    out.write(data[i:i + step])
    out.flush()
if os.environ.get("FAKE_HANG"):
    # Stay alive holding the card, so stop() has something real to kill.
    while True:
        time.sleep(0.05)
'''


@pytest.fixture
def arecord(tmp_path, monkeypatch):
    """Write the fake arecord and return a function that arms it.

        exe = arecord(pcm, FAKE_HANG=1)

    The environment goes through monkeypatch, so one test's FAKE_FAIL never
    reaches the next one's capture.
    """
    def arm(pcm: bytes, params: str = SEVEN_HW_PARAMS, **env) -> str:
        path = tmp_path / "arecord"
        path.write_text(FAKE_ARECORD)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        (tmp_path / "pcm.raw").write_bytes(pcm)
        monkeypatch.setenv("FAKE_PARAMS", params)
        monkeypatch.setenv("FAKE_PCM", str(tmp_path / "pcm.raw"))
        monkeypatch.setenv("FAKE_RUNS", str(tmp_path / "runs"))
        for k in ("FAKE_FAIL", "FAKE_HANG", "FAKE_CHUNK"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        return str(path)

    return arm


async def drain(audio: UsbAudio, want: int, limit: float = 5.0) -> None:
    """Wait until at least `want` blocks have arrived, or give up."""
    for _ in range(int(limit / 0.02)):
        if audio.blocks >= want:
            return
        await asyncio.sleep(0.02)


def test_the_capture_delivers_each_receiver_exactly(arecord, tmp_path):
    # Three seconds, not one: `Levels` refuses to say whether the two
    # channels differ until it has compared enough pairs to mean it, and a
    # test that ran under that threshold would be testing the refusal.
    stereo = interleave(noise(144000, seed=11), noise(144000, seed=12))
    exe = arecord(stereo, FAKE_CHUNK=7919, FAKE_HANG=1)

    async def run():
        got: dict[str, bytearray] = {}
        audio = UsbAudio("hw:9,0", on_pcm=lambda rx, pcm: got.setdefault(
            rx, bytearray()).__iadd__(pcm), arecord=exe)
        await audio.start()
        try:
            assert audio.format is not None and audio.format.rate == 48000 \
                and audio.format.channels == 2, \
                f"the format came from what the card said: {audio.format}"
            await drain(audio, len(stereo) // audio.format.block_bytes())
            # Give the last partial block a moment.
            await asyncio.sleep(0.2)

            assert sorted(got) == ["left", "right"], \
                f"audio arrives named by receiver: {sorted(got)}"
            want = slowly(stereo, 2, 4)
            # The bytes, not the count. A capture that delivered the right
            # amount of the wrong samples is the failure this whole file is
            # about, and it is the one a length check passes.
            assert got["left"] and bytes(got["left"]) == want[0][: len(got["left"])], \
                f"the left channel is exactly the left channel: {len(got['left'])} bytes"
            assert got["right"] and bytes(got["right"]) == want[1][: len(got["right"])], \
                "and the right one is the right one"
            stats = audio.stats()
            assert stats["rate"] == DECODE_RATE, \
                "delivered at 12 kHz, which is what the decoders read"
            assert "two receivers" in stats["what_the_radio_is_sending"], \
                f"and it says two receivers are on the cable: {stats.get('what_the_radio_is_sending')}"
            assert audio.primary() == "left", f"the primary receiver is named: {audio.primary()}"

            # What the argv really is. `plughw` here would be the 2.1% bug,
            # and it is the kind of thing a later edit puts back "to make it
            # work on a card that refused". It is checked at the argument.
            argv = (tmp_path / "runs").read_text().split("\n")[0]
            assert "plughw" not in argv and "hw:9,0" in argv, \
                f"the capture asks the hardware, not the plug layer: {argv}"
            assert "-r 48000" in argv, f"and asks for the card's own rate: {argv}"
            assert "-c 2" in argv, f"in the card's own channel count: {argv}"
        finally:
            proc = audio._proc
            await audio.stop()
        assert proc is not None and proc.returncode is not None, \
            "stopping it leaves nothing holding the card: " \
            f"{proc.returncode if proc else 'no process'}"

    asyncio.run(run())


def test_a_capture_that_ends_is_started_again(arecord, tmp_path, monkeypatch):
    # Because a radio switched off at the wall takes its sound card with it
    # and comes back later with nobody there.
    exe = arecord(noise(4800, seed=5))
    monkeypatch.setattr(usbaudio, "BACKOFF_START", 0.05)

    async def run():
        audio = UsbAudio("hw:9,0", arecord=exe)
        await audio.start()
        try:
            for _ in range(100):
                if audio.restarts >= 2:
                    break
                await asyncio.sleep(0.05)
            assert audio.restarts >= 2, f"a capture that ends is started again: {audio.restarts}"
            runs = (tmp_path / "runs").read_text().strip().split("\n")
            # The subprocess really ran more than once, rather than a counter
            # having been incremented by something that never spawned it.
            assert len(runs) >= 2, f"and it really ran again: {len(runs)}"
        finally:
            await audio.stop()

    asyncio.run(run())


def test_a_card_that_will_not_open_reports_arecords_own_words(arecord, monkeypatch):
    # In the stats, where somebody three hundred miles away can read it.
    exe = arecord(b"", FAKE_FAIL=1)
    monkeypatch.setattr(usbaudio, "BACKOFF_START", 30.0)

    async def run():
        audio = UsbAudio("hw:9,0", arecord=exe)
        await audio.start()
        try:
            for _ in range(100):
                if audio.error:
                    break
                await asyncio.sleep(0.02)
            assert "Device or resource busy" in (audio.error or ""), str(audio.error)
        finally:
            await audio.stop()

    asyncio.run(run())


def test_a_missing_sound_card_names_the_thing_to_do():
    # And the link stays up: a radio that can be controlled and not heard is
    # a state worth being in, and it is the state the appliance ships in.
    async def run():
        audio = UsbAudio("/dev/snd/by-id/nothing-here")
        await audio.start()
        try:
            assert "Devices" in (audio.error or ""), str(audio.error)
            assert audio.receivers() == (), \
                "and reports no receivers rather than guessing two"
        finally:
            await audio.stop()

    asyncio.run(run())


# -- the whole path, against a signal nobody here made -----------------------


def _off_air_through_the_splitter(rec: Path):
    """Real 40 m, up to 48 kHz stereo and back down, as each receiver.

    The recording is turned into what the codec would have delivered - 48 kHz,
    two channels, one of them carrying something else - and pushed through the
    real splitter in blocks of an awkward size. The other channel is the same
    band reversed: real receiver noise at a real level, with nothing in it a
    decoder can find. A silent channel would not prove the two do not leak
    into one another.
    """
    mono12k = rec.read_bytes()[44:]
    at48k = upsample4(mono12k)
    backwards = array.array("h")
    backwards.frombytes(mono12k)
    backwards.reverse()
    back12k = backwards.tobytes()
    other48k = upsample4(back12k)

    for wanted, (l, r) in (("left", (at48k, other48k)), ("right", (other48k, at48k))):
        s = Split(2, 48000)
        acc = {"left": bytearray(), "right": bytearray()}
        # 7919 bytes is prime and is not a whole number of frames, let alone a
        # whole decimation group - which is what a pipe hands over.
        stereo = interleave(l, r)
        for i in range(0, len(stereo), 7919):
            out = s.feed(stereo[i:i + 7919])
            acc["left"] += out[0]
            acc["right"] += out[1]
        yield wanted, mono12k, back12k, acc


def test_off_air_audio_comes_back_sample_for_sample():
    """The jt9-free half of the app's off-air check, and a stricter one.

    `upsample4` interpolates, so only phase 0 of the decimation recovers the
    recording exactly; a splitter that lost its place at a block boundary
    would hand back something that still decodes, slightly worse. So the
    samples, not a decode, are compared: each receiver is the recording to the
    byte, and the other receiver is the reversed copy, not a leak of this one.
    """
    rec = off_air()
    if rec is None:
        pytest.skip(f"set SHACK_RIG_OFF_AIR to the Icom app's {OFF_AIR_NAME} "
                    "(icom/app/digi/offair/); it is not copied into ohs-libs")
    for wanted, mono12k, back12k, acc in _off_air_through_the_splitter(rec):
        other = "right" if wanted == "left" else "left"
        pcm = bytes(acc[wanted])
        assert abs(len(pcm) - len(mono12k)) <= 8, \
            f"the {wanted} channel comes back the length it went in as: {len(pcm)} against {len(mono12k)}"
        n = min(len(pcm), len(mono12k))
        assert pcm[:n] == mono12k[:n], f"the {wanted} channel is the recording, sample for sample"
        assert bytes(acc[other])[:n] == back12k[:n], \
            f"and the {other} channel is its own audio, not a copy of the {wanted}"


def test_off_air_signals_decode_at_the_right_frequencies():
    """The check the app's version of this file exists to support.

    What it compares is the **frequencies** jt9 reports, because that is what
    a wrong sample rate moves and nothing else would notice: all three signals
    in the recording would still decode with the audio 2.1% high, at 2886,
    1049 and 919 Hz instead of 2827, 1027 and 900.

    Not ported: it decodes through the Icom app's `app.digi.decoders`, which
    is the Digital Modes side and not part of shack_rig. The splitter half of
    it is test_off_air_audio_comes_back_sample_for_sample above, and
    test_the_rate_is_exact covers the frequency arithmetic without a decoder.
    """
    if shutil.which("jt9") is None:
        pytest.skip("jt9 is not installed")
    pytest.skip("needs the Digital Modes decoder (app.digi.decoders), which is "
                "not part of shack_rig; runs in the Icom app's selftest_usbaudio.py")
