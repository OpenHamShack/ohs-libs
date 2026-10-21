"""G3RUH FSK against direwolf, both ways: its `gen_packets -B` into our
receiver, our transmitter into its `atest -B`, plain AX.25 and FX.25, at
9600, 19 200 and 38 400 baud. Skipped without direwolf's tools; audio goes
under tmp_path (~/build via tools/test.sh), never /tmp."""

from __future__ import annotations

import subprocess

import pytest
from helpers import need
from test_direwolf import CORPUS, atest, monitor_lines

from shack_packet import ax25, bench, fsk

#: direwolf's own rates for each speed: 48 kHz is its 9600 default (five
#: samples a bit); it takes up to 192 kHz.
RATES = {9600: 48_000, 19_200: 96_000, 38_400: 192_000}


def rx(path, baud):
    rate, audio = bench.read_wav(path)
    r = fsk.Receiver(rate, baud)
    return r.feed(audio) + r.flush()


@pytest.mark.parametrize("baud", sorted(RATES))
@pytest.mark.parametrize("fx", [None, "16", "64"])
def test_direwolfs_fsk_decodes_packet_for_packet(tmp_path, baud, fx):
    need("gen_packets")
    source = tmp_path / "packets.txt"
    source.write_text("\n".join(CORPUS) + "\n", encoding="latin-1")
    wav = tmp_path / "corpus.wav"
    subprocess.run(["gen_packets", "-B", str(baud), "-r", str(RATES[baud]), "-o", str(wav)]
                   + (["-X", fx] if fx else []) + [str(source)], check=True, capture_output=True)
    heard = rx(wav, baud)
    assert monitor_lines(heard) == CORPUS
    if fx:
        assert all(h.fx25 is not None for h in heard)


@pytest.mark.parametrize("baud", sorted(RATES))
@pytest.mark.parametrize("mode", [None, 32])
def test_our_fsk_decodes_in_direwolf(tmp_path, baud, mode):
    need("atest")
    frames = [ax25.Frame.from_monitor(line).encode() for line in CORPUS]
    wav = tmp_path / "ours.wav"
    rate = RATES[baud]
    bench.write_wav(wav, fsk.modulate(frames, rate, baud=baud, fx25_mode=mode), rate)
    count, text = atest(wav, "-B", str(baud), *(["-d", "x"] if mode else []))
    assert count == len(CORPUS), text[-2000:]
    for line in CORPUS:
        assert line in text, line
    if mode:
        assert text.count("FX.25") >= len(CORPUS), text[-2000:]


def test_direwolfs_noisy_ramp_we_match_it(tmp_path):
    """`gen_packets -n 20` sends twenty frames into rising noise. Measured
    18/10/2026: atest decoded 16 (17 with -P +) at 9600, and so do we; at
    19 200 15 against our 16; at 38 400 (five samples a bit, at 192 kHz)
    12 against our 13. Held here at no worse than direwolf's default less
    one: the number moves with the noise, not with us."""
    need("gen_packets")
    need("atest")
    for baud in (9600, 19_200, 38_400):
        wav = tmp_path / f"ramp{baud}.wav"
        rate = min(10 * baud, 192_000)
        subprocess.run(["gen_packets", "-B", str(baud), "-r", str(rate), "-n", "20", "-o", str(wav)],
                       check=True, capture_output=True)
        theirs, _ = atest(wav, "-B", str(baud))
        ours = len(rx(wav, baud))
        assert ours >= theirs - 1, (baud, ours, theirs)
