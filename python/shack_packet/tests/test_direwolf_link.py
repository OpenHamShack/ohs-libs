"""The engine against direwolf 1.7, the AX.25 2.2 implementation most
stations meet. Skipped without direwolf (apt install direwolf): a
development check, not a dependency.

Offline: every frame two engines exchange in a session (SABM/SABME, UA,
XID, I, RR, REJ, DISC, at both moduli), and the UI frames of APRS, IP and
ARP, are modulated, decoded by `atest -h` and compared byte for byte, with
direwolf's name for each frame's type.

Live: direwolf runs as a real station on audio pipes (direwolf_live.py: UDP
in, a FIFO out through ALSA's user-space file plugin; no sound device), and
the engine holds connected-mode sessions with it over our modem and its,
both ways round and at both moduli, driven through direwolf's AGW port as
an application would. Each live test takes 10-30 s of wall time: the
channel is 1200 baud in real time.
"""

from __future__ import annotations

import re
import subprocess
import time

import pytest
from helpers import need
from ippackets import icmp_echo
from stations import World, accept_all

from shack_packet import afsk, aprs, ax25, bench
from shack_packet.aprs_station import AprsConfig, AprsStation, Beacon
from shack_packet.datalink import LinkConfig, State
from shack_packet.engine import Engine, Runner
from shack_packet.identity import Station
from shack_packet.ip import IpConfig, IpInterface

ATEST_TYPE = re.compile(r"^([UIS]) frame(?: (\w+))?:")


def atest_hex(wav) -> list[tuple[str, bytes]]:
    """atest -h: each decoded frame's type as direwolf names it, and its
    bytes."""
    out = subprocess.run(["atest", "-h", str(wav)], capture_output=True, timeout=300, check=False)
    text = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout.decode("latin-1"))
    frames: list[tuple[str, bytearray]] = []
    for line in text.splitlines():
        match = ATEST_TYPE.match(line)
        if match:
            frames.append((match.group(2) or match.group(1), bytearray()))
            continue
        dump = re.match(r"^  ([0-9a-f]{3}):  ((?:[0-9a-f]{2} )+)", line)
        if dump and frames:
            frames[-1][1].extend(bytes.fromhex(dump.group(2)))
    return [(kind, bytes(body)) for kind, body in frames]


def session_frames(modulo: int) -> list[bytes]:
    """Every frame of a short session with a lost frame in it: connect,
    1.5 kB each way, disconnect."""
    world = World(loss=0.08, seed=modulo + 1)
    a = world.station("G0ABC", link=LinkConfig(modulo=modulo))
    b = world.station("GM4BRI")
    sent: list[bytes] = []
    for engine in (a, b):
        engine.channel.on_transmit = lambda start, end, frames: sent.extend(frames)
    accepted = accept_all(b)
    conn = a.connect("GM4BRI")
    assert world.run(60, lambda: conn.connected)
    conn.send(bytes(range(256)) * 6)
    assert world.run(300, lambda: len(accepted[0][1]) >= 1536)
    accepted[0][0].send(b"thanks " * 200)
    assert world.run(300, lambda: conn.received_bytes >= 1400)
    conn.close()
    assert world.run(60, lambda: conn.state is State.DISCONNECTED)
    return sent


@pytest.mark.parametrize("modulo", [8, 128])
def test_every_frame_of_a_session_decodes_identically_in_atest(tmp_path, modulo):
    need("atest")
    sent = session_frames(modulo)
    wav = tmp_path / "session.wav"
    bench.write_wav(wav, afsk.modulate(sent, 48_000), 48_000)
    decoded = atest_hex(wav)
    assert [body for _, body in decoded] == sent
    for (kind, body) in decoded:
        ours = ax25.parse(body, modulo)
        if ours.type.value in ("SABM", "SABME", "UA", "DM", "DISC", "XID", "UI", "FRMR", "TEST"):
            assert kind == ours.type.value
        elif modulo == 8:
            assert kind == (ours.type.value if ours.type.value != "I" else "I")
    kinds = {ax25.parse(f, modulo).type.value for f in sent}
    assert {"UA", "I", "RR", "DISC"} <= kinds
    assert ("SABME" if modulo == 128 else "SABM") in kinds


def test_ui_aprs_ip_and_arp_frames_decode_identically_in_atest(tmp_path):
    """The connectionless frames the services send - an APRS beacon and
    message, an IP datagram (PID 0xCC) and an ARP request (0xCD) - as the
    engine queued them."""
    need("atest")
    world = World()
    a = world.station("G0ABC")
    b = world.station("GM4BRI")
    sent: list[bytes] = []
    a.channel.on_transmit = lambda start, end, frames: sent.extend(frames)
    aprs_station = AprsStation(a, AprsConfig(beacons=[Beacon(aprs.format_position(55.6, -4.3, "/-", "test"))],
                                             first_delay_s=(1.0, 1.0)))
    aprs_station.send_message("GM4BRI", "hello")
    interface = IpInterface(a, IpConfig("44.131.1.1", "44.131.0.0/16"))
    interface.add_arp("44.131.1.2", "GM4BRI")
    interface.send(icmp_echo("44.131.1.1", "44.131.1.2"))
    interface.send(icmp_echo("44.131.1.1", "44.131.1.3"))       # unknown: ARP first
    world.run(20)
    wav = tmp_path / "ui.wav"
    bench.write_wav(wav, afsk.modulate(sent, 48_000), 48_000)
    decoded = atest_hex(wav)
    assert [body for _, body in decoded] == sent
    pids = sorted(ax25.parse(f).pid for f in sent)
    assert pids.count(0xF0) >= 2 and 0xCC in pids and 0xCD in pids
    assert all(kind == "UI" for kind, _ in decoded)


# -- live --------------------------------------------------------------------------


@pytest.fixture
def direwolf(tmp_path):
    need("direwolf")
    need("stdbuf")
    from direwolf_live import Direwolf
    made = []

    def start(mycall="M0TST", extra="", modulo=8, k=None):
        dw = Direwolf(tmp_path, mycall, extra)
        link = dw.link()
        engine = Engine(Station.of("G0ABC", tx_enabled=True), link, link=LinkConfig(modulo=modulo, k=k))
        link.start()
        runner = Runner(engine).start()
        made.append((dw, link, runner))
        dw.start()
        dw.register(mycall)
        return dw, engine
    yield start
    for dw, link, runner in made:
        runner.stop()
        dw.stop()
        link.close()


def _wait(predicate, timeout_s):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _agw_data(dw) -> bytes:
    dw.poll(0.05)
    return b"".join(data for kind, _, data in dw.frames if kind == "D")


def test_direwolf_connects_to_us_with_sabme_and_xid_and_we_carry_its_data(direwolf):
    dw, engine = direwolf()
    accepted = accept_all(engine)
    dw.send("C", "M0TST", "G0ABC")
    assert dw.wait_for(lambda f: f[0] == "C", 30), dw.log()[-2000:]
    conn, got, events = accepted[0]
    assert conn.modulo == 128, "direwolf opens 2.2 links with SABME"
    text = b"The quick brown fox jumps over the lazy dog. " * 12
    dw.send("D", "M0TST", "G0ABC", text, pid=0xF0)
    assert _wait(lambda: bytes(got) == text, 60), (len(got), dw.log()[-2000:])
    conn.send(b"73 de G0ABC " * 40)
    assert _wait(lambda: _agw_data(dw) == b"73 de G0ABC " * 40, 60), dw.log()[-2000:]
    assert conn.link.stats["xid_applied"] >= 1
    # direwolf heard our XID response and parsed it.
    assert "G0ABC>M0TST:(XID res" in dw.log()
    dw.send("d", "M0TST", "G0ABC")
    assert _wait(lambda: conn.state is State.DISCONNECTED, 30)
    assert events[-1].reason == "closed-by-peer"


@pytest.mark.parametrize("modulo", [8, 128])
def test_we_connect_to_direwolf_and_close(direwolf, modulo):
    dw, engine = direwolf(modulo=modulo)
    conn = engine.connect("M0TST")
    assert _wait(lambda: conn.connected, 40), dw.log()[-2000:]
    assert conn.modulo == modulo
    assert dw.wait_for(lambda f: f[0] == "C", 10)
    payload = bytes(range(32, 127)) * 12                     # 1140 bytes, several I frames
    conn.send(payload)
    assert _wait(lambda: _agw_data(dw) == payload, 90), dw.log()[-3000:]
    dw.send("D", "M0TST", "G0ABC", b"received, thanks\r", pid=0xF0)
    got = bytearray()
    conn.on_data = got.extend
    got += conn.read()
    assert _wait(lambda: bytes(got) == b"received, thanks\r", 40), dw.log()[-2000:]
    if modulo == 128:
        # We sent XID after UA; direwolf answered with its parameters.
        assert _wait(lambda: conn.link.stats["xid_applied"] >= 1, 20), dw.log()[-2000:]
    conn.close()
    assert _wait(lambda: conn.state is State.DISCONNECTED, 40), dw.log()[-2000:]
    assert conn.reason == "closed"
    assert dw.wait_for(lambda f: f[0] == "d", 10)
