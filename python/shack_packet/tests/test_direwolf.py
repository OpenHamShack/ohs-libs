"""Interop with direwolf, the reference implementation, both ways: its
transmitter into our receiver, our transmitter into its receiver, plain
AX.25 and FX.25. Every test skips without direwolf's tools (apt install
direwolf); they are a development check, not a dependency.

Audio goes under ~/build (tmp_path, from tools/test.sh's --basetemp), not
/tmp."""

from __future__ import annotations

import re
import subprocess

import pytest
from helpers import need

from shack_packet import afsk, aprs, ax25, bench, fx25

#: The packets this is checked against, chosen for what turns up on 144.800
#: rather than for what is easy (the Icom selftest's corpus).
CORPUS = [
    "GM0ABC-9>APDW17,WIDE1-1,WIDE2-1:!5537.14N/00420.83W>Test position",
    "MM0XYZ>APDW17,GB7AY*,WIDE2-1:@092345z5537.14N/00420.83W-House",
    "G4ABC-1>APDW17:=5537.14N/00420.83W#PHG5360 Digipeater",
    "M0ABC-9>TQPX2X,WIDE1-1:`(_fn\"Oj/]\"4-}Testing mic-E",
    "2E0TST-7>APDW12:>Monitoring 144.800 in Ayrshire",
    "G0WX-13>APDW17:_10090556c220s004g005t077r000p000P000h50b09900",
    "GM0ABC>APDW17::GM7XYZ   :Are you around this evening?{12",
    "GM0ABC>APDW17:;LEADHILLS*092345z5525.00N/00337.00WrSOTA GM/SS-176",
    "GM0ABC-9>APDW17:!/5L!!<*e7> sT compressed",
]


def monitor_lines(heard):
    # gen_packets puts a newline on the end of every information field from
    # its input file; that is its input's, not the packet's.
    return [ax25.parse(h.frame).monitor().rstrip("\r\n") for h in heard]


def atest(path, *args) -> tuple[int, str]:
    run = subprocess.run(["atest", *args, str(path)], capture_output=True, timeout=300, check=False)
    text = re.sub(r"\x1b\[[0-9;]*m", "", run.stdout.decode("latin-1", "replace"))
    count = 0
    for line in text.splitlines():
        match = re.match(r"^(\d+) from ", line.strip())
        if match:
            count = int(match.group(1))
    return count, text


@pytest.mark.parametrize("rate", [12_000, 44_100, 48_000])
@pytest.mark.parametrize("fx", [None, "16", "32", "64"])
def test_direwolfs_audio_decodes_packet_for_packet(tmp_path, rate, fx):
    need("gen_packets")
    source = tmp_path / "packets.txt"
    source.write_text("\n".join(CORPUS) + "\n", encoding="latin-1")
    wav = tmp_path / "corpus.wav"
    args = ["gen_packets", "-r", str(rate), "-o", str(wav)] + (["-X", fx] if fx else []) + [str(source)]
    subprocess.run(args, check=True, capture_output=True)
    got_rate, audio = bench.read_wav(wav)
    assert got_rate == rate
    heard = bench.decode(audio, rate)
    assert monitor_lines(heard) == CORPUS
    if fx:
        assert all(h.fx25 is not None for h in heard), "FX.25 sent, but not reported as FX.25"
        assert {fx25.TAGS[h.fx25].nroots for h in heard} == {int(fx)}


@pytest.mark.parametrize("rate", [8000, 12_000, 44_100, 48_000])
@pytest.mark.parametrize("mode", [None, 16, 32, 64])
def test_our_audio_decodes_in_direwolf(tmp_path, rate, mode):
    need("atest")
    frames = [ax25.Frame.from_monitor(line).encode() for line in CORPUS]
    wav = tmp_path / "ours.wav"
    bench.write_wav(wav, afsk.modulate(frames, rate, fx25_mode=mode), rate)
    count, text = atest(wav, "-d", "x") if mode else atest(wav)
    assert count == len(CORPUS), text[-2000:]
    for line in CORPUS:
        assert line in text, line
    if mode:
        # atest -d x reports each FX.25 block it corrected.
        assert text.count("FEC complete") == len(CORPUS), text[-2000:]


def test_on_direwolfs_noise_ramp_we_decode_at_least_what_direwolf_does(tmp_path):
    """The measurement the Icom code was judged by: gen_packets -n 30 at
    five amplitudes, 12 kHz. On 17/10/2026, direwolf 1.7: atest 54 (57 with
    -P E+), this receiver 61, the Icom one 36. Asserted at 90 % of direwolf
    so a newer direwolf moving a frame or two does not fail a release; the
    bench prints the numbers."""
    need("gen_packets")
    need("atest")
    ours = theirs = 0
    for amplitude in bench.AMPLITUDES:
        wav = tmp_path / f"ramp{amplitude}.wav"
        subprocess.run(["gen_packets", "-r", "12000", "-n", "30", "-a", str(amplitude), "-o", str(wav)],
                       check=True, capture_output=True)
        theirs += atest(wav)[0]
        rate, audio = bench.read_wav(wav)
        ours += len(bench.decode(audio, rate))
    assert theirs > 0
    assert ours >= 0.9 * theirs, f"here {ours}, direwolf {theirs}"


def test_direwolf_reads_the_same_positions_from_the_payloads(tmp_path):
    """decode_aprs is direwolf's payload parser as a command line program;
    positions are compared to 1e-5 degrees (about a metre)."""
    need("decode_aprs")
    source = tmp_path / "corpus.txt"
    source.write_text("\n".join(CORPUS) + "\n", encoding="latin-1")
    text = subprocess.run(["decode_aprs", str(source)], check=True, capture_output=True,
                          timeout=120).stdout.decode("latin-1")
    theirs = []
    for match in re.finditer(r"([NS]) (\d+) ([\d.]+), ([EW]) (\d+) ([\d.]+)", text):
        ns, d1, m1, ew, d2, m2 = match.groups()
        theirs.append(((int(d1) + float(m1) / 60) * (1 if ns == "N" else -1),
                       (int(d2) + float(m2) / 60) * (1 if ew == "E" else -1)))
    ours = []
    for line in CORPUS:
        packet = aprs.parse(ax25.Frame.from_monitor(line))
        if packet.position is not None:
            ours.append((packet.position.lat, packet.position.lon))
    assert len(theirs) == len(ours)
    for (lat, lon), (mine_lat, mine_lon) in zip(theirs, ours):
        assert abs(lat - mine_lat) < 1e-5 and abs(lon - mine_lon) < 1e-5


def test_direwolf_reads_what_our_aprs_station_writes(tmp_path):
    """The encoders the APRS station beacons and messages with, read back
    by decode_aprs: position, speed, course and altitude, a numbered
    message, its ack, a status."""
    need("decode_aprs")
    lines = [
        "GM4BRI>APZOHS,WIDE1-1:" + aprs.format_position(55.619, -4.347, "/-", "home", course_deg=90,
                                                        speed_kmh=50, altitude_m=100),
        "GM4BRI>APZOHS:" + aprs.format_message("G0ABC-9", "QSY 145.500?", "42"),
        "GM4BRI>APZOHS:" + aprs.format_ack("G0ABC-9", "42"),
        "GM4BRI>APZOHS:" + aprs.format_status("on 144.800"),
    ]
    source = tmp_path / "ours.txt"
    source.write_text("\n".join(lines) + "\n", encoding="latin-1")
    text = re.sub(r"\x1b\[[0-9;]*m", "", subprocess.run(["decode_aprs", str(source)], check=True,
                                                        capture_output=True, timeout=120).stdout.decode("latin-1"))
    assert "N 55 37.1400, W 004 20.8200, 50 km/h (31 MPH), course 90, alt 100 m" in text
    assert 'APRS Message, number "42", from "GM4BRI" to "G0ABC-9"' in text
    assert '"GM4BRI" ACKnowledged message number "42" from "G0ABC-9"' in text
    assert "Status Report" in text
