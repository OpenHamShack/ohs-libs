"""A link test end to end: two engines on fake Plutos joined by one RF
direction (sender to receiver), the relay (tools/packet_relay.py) running
here on a port of its own, the receiver's answers through it
(linktest.RelayReturnPath) and both stations' telemetry into its log -
the path Brian and GM0OTB use on the air, before they do."""

from __future__ import annotations

import asyncio
import collections
import importlib.util
import json
import random
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from sdrhelp import FakePluto

from shack_packet.channel import ChannelConfig
from shack_packet.datalink import LinkConfig, State
from shack_packet.engine import Engine, Runner
from shack_packet.identity import Station
from shack_packet.linktest import LinkTestError, RelayClient, RelayReturnPath, Telemetry, parse_address
from shack_packet.radios import PlutoLink
from shack_packet.radios.pluto import FSK_RATE_IQ

TOOLS = Path(__file__).parent.parent / "tools"
SENDER, RECEIVER = "GM4BRI-1", "GM0OTB-1"
FREQ = 1299.375e6
MODE = "g3ruh38400"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


relay_mod = _load("packet_relay")


class RelayThread:
    """The relay on 127.0.0.1 and a free port, in a thread with its own
    event loop; `kill()` drops it as a box restart would: the listener and
    every connection closed at once, no goodbyes."""

    def __init__(self, log_dir: Path, silence_s: float = 5.0):
        self.relay = relay_mod.Relay(log_dir, silence_s)
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(self.relay.serve("127.0.0.1", 0))
        self.port = self.server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()

    def kill(self):
        def go():
            self.server.close()
            for sess in list(self.relay.sessions.values()):
                for c in list(sess.clients.values()):
                    c.writer.transport.abort()
        self.loop.call_soon_threadsafe(go)

    def stop(self):
        self.kill()
        time.sleep(0.2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


class Air:
    """One RF direction: what the sender's fake Pluto pushes with its TX LO
    up reaches the receiver's, in order, in white noise; both receivers hear
    noise otherwise. As test_fastfsk_tool's: the listener starts on an over
    once 0.2 s of it is waiting or it has ended, so its real-time reader
    never overtakes the talker's real-time writer."""

    def __init__(self, sigma=0.01, seed=1):
        self.q = collections.deque()
        self.have = 0
        self.flowing = False
        self.lock = threading.Lock()
        self.rng = np.random.default_rng(seed)
        self.sigma = sigma
        self.last_push = 0.0

    def pushed(self, raw: bytes, live: bool) -> None:
        if live:
            x = (np.frombuffer(raw, np.int16) >> 4).astype(np.int16)
            with self.lock:
                self.q.append(x)
                self.have += len(x)
                self.last_push = time.monotonic()

    def read(self, n: int) -> np.ndarray:
        out = np.round(self.rng.normal(0, self.sigma * 2047, n)).astype(np.int16)
        with self.lock:
            ended = self.have and time.monotonic() - self.last_push > 0.1
            if not self.flowing and (self.have >= 2 * int(0.2 * FSK_RATE_IQ) or ended):
                self.flowing = True
            if not self.flowing:
                return out
            at = 0
            while at < n and self.q:
                head = self.q[0]
                take = min(n - at, len(head))
                out[at:at + take] += head[:take]
                at += take
                self.have -= take
                if take == len(head):
                    self.q.popleft()
                else:
                    self.q[0] = head[take:]
            if at < n:
                self.flowing = False
        return out


class Pushed(list):
    def __init__(self, air):
        super().__init__()
        self.air = air

    def append(self, item):
        super().append(item)
        if self.air is not None:
            self.air.pushed(*item)


class Side:
    """One station: fake Pluto, PlutoLink, engine, relay client, return
    path and telemetry."""

    def __init__(self, call, peer, role, pluto, port, log_dir, transmit, ladder=None):
        self.call, self.role, self.pluto = call, role, pluto
        self.station = Station.of(call, tx_enabled=transmit)
        modem = {"modes": ladder} if ladder else {"mode": MODE}
        self.link = PlutoLink("ip:192.168.2.1", FREQ, **modem, iio_module=pluto.module(), station=self.station,
                              transmit=transmit, allow_unguarded=transmit, max_key_s=8.0)
        self.link.open()
        cfg = ChannelConfig(txdelay_s=0.15, txtail_s=0.01, max_tx_s=6.0, mode=None if ladder else MODE,
                            max_frames=33)
        self.engine = Engine(self.station, self.link, channel=cfg,
                             link=LinkConfig(k=32, n1=512, modulo=128, srej=True))
        self.client = RelayClient("127.0.0.1", port, session="bri-ray-test", role=role, call=call, peer=peer,
                                  env={"app": "test"}, log_path=log_dir / f"local-{role}.jsonl")
        self.events = []
        self.engine.on_event = self.events.append
        self.runner = Runner(self.engine).start()
        assert self.link.rx_listening.wait(10)

    def join(self):
        self.client.start()
        self.path = RelayReturnPath(self.client, self.client.peer, outbound=self.role == "rf-receiver")
        # Telemetry first, so the log has the return path switching on.
        self.tele = Telemetry(self.engine, self.client, self.link).start()
        self.engine.attach_return_path(self.path)

    def close(self):
        self.tele.stop()
        self.client.close()
        self.runner.stop()
        self.link.close()


@pytest.fixture
def pair(tmp_path, request):
    ladder = getattr(request, "param", None)
    relay = RelayThread(tmp_path / "sessions")
    a, b = FakePluto(), FakePluto()
    air = Air(seed=1)
    a.pushed, b.pushed = Pushed(air), Pushed(None)
    b.rx_source = air.read
    a.rx_source = Air(seed=2).read                 # the sender's RX port has no antenna: noise
    a.rx_realtime = b.rx_realtime = True
    sender = Side(SENDER, RECEIVER, "rf-sender", a, relay.port, tmp_path, transmit=True, ladder=ladder)
    receiver = Side(RECEIVER, SENDER, "rf-receiver", b, relay.port, tmp_path, transmit=False, ladder=ladder)
    receiver.join()
    sender.join()
    assert _until(lambda: sender.client.ready and receiver.client.ready, 5)
    yield sender, receiver, relay, tmp_path
    for side in (sender, receiver):
        try:
            side.close()
        except Exception:                           # noqa: BLE001 - a test that failed half-way
            pass
    relay.stop()


def _until(pred, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def _payload(n, seed=3):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(n))


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_100_kb_transfer_with_the_answers_by_relay(pair):
    sender, receiver, relay, tmp = pair
    got = bytearray()
    receiver.engine.listen(RECEIVER, lambda call: (lambda conn: setattr(conn, "on_data", got.extend)))
    conn = sender.engine.connect(RECEIVER)
    assert _until(lambda: conn.connected, 20), sender.events
    data = _payload(100 * 1024)
    t0 = time.monotonic()
    conn.send(data)
    assert _until(lambda: len(got) >= len(data) and conn.pending == 0, 240), (len(got), sender.link.stats)
    took = time.monotonic() - t0
    assert bytes(got) == data
    conn.close()
    assert _until(lambda: conn.state is State.DISCONNECTED, 20)
    # One RF direction: the receiver's Pluto never pushed a sample with its
    # TX LO up, and its engine never keyed; everything it said went by relay.
    assert not [p for p in receiver.pluto.pushed if p[1]] and receiver.pluto.tx_quiet()
    assert receiver.engine.channel.stats.transmissions == 0
    assert receiver.engine.stats["rp_sent"] > 0
    assert sender.engine.stats["rp_heard"] == receiver.engine.stats["rp_sent"]
    assert sender.link.stats["keyed"] > 0
    print(f"100 KB in {took:.1f} s, {len(data) * 8 / took:.0f} bit/s; "
          f"{receiver.engine.stats['rp_sent']} answers by relay; {sender.engine.channel.stats.transmissions} overs")
    for side in (sender, receiver):
        side.close()
    assert _until(lambda: not relay.relay.sessions, 5)
    logs = list((tmp / "sessions").glob("*-bri-ray-test.jsonl"))
    assert len(logs) == 1
    recs = _records(logs[0])
    # Every line: the relay's two clocks, who from, the message; client
    # messages carry both of theirs, and the relay's lag estimate.
    for r in recs:
        assert {"relay_wall", "relay_mono", "from", "msg"} <= set(r)
        if r["from"] != "relay":
            assert {"mono", "wall"} <= set(r["msg"]) and "lag_s" in r
    by = collections.defaultdict(list)
    for r in recs:
        if r["msg"].get("t") == "tele":
            by[(r["from"], r["msg"]["kind"])].append(r["msg"])
    frames = [r for r in recs if r["msg"].get("t") == "frame"]
    assert len(frames) == receiver.engine.stats["rp_sent"] and {r["from"] for r in frames} == {"rf-receiver"}
    rx = by[("rf-receiver", "rx-frame")]
    radio_rx = [m for m in rx if m["via"] == "radio"]
    assert radio_rx and all(m["mode"] == MODE and m["fcs"] == "ok" and m["type"] for m in radio_rx)
    assert {k for m in radio_rx for k in m} >= {"level_db", "snr_db", "cn0_dbhz", "offset_hz", "decode_lag_s",
                                                "ns", "nr", "len", "frame"}
    assert {m["via"] for m in by[("rf-sender", "rx-frame")]} == {"internet"}
    overs = by[("rf-sender", "tx-over")]
    assert overs and all(m["frames"] > 0 and m["duration_s"] > 0 and m["mode"] == MODE for m in overs)
    assert {"counters", "tx_slack_min_ms", "tx_gain_db", "xo", "temp_c", "guard"} <= set(overs[0])
    assert by[("rf-sender", "env")][0]["radio"]["kind"] == "PlutoLink"
    assert by[("rf-receiver", "env")][0]["radio"]["transmit"] is False
    assert by[("rf-sender", "link-stats")] and by[("rf-receiver", "rx")]
    assert any(m["event"] == "return-path" and m["reason"] == "on" for m in by[("rf-sender", "link")])
    summary = json.loads(logs[0].with_name(logs[0].stem + ".summary.json").read_text())
    assert summary["frames"] == {"rf-sender->rf-receiver": 0, "rf-receiver->rf-sender": len(frames)}
    assert summary["calls"] == {"rf-sender": SENDER, "rf-receiver": RECEIVER}
    assert recs[-1]["msg"]["t"] == "summary"
    # And each station kept its own copy of everything it sent and heard.
    local = _records(tmp / "local-rf-sender.jsonl")
    assert {"in", "out"} <= {r["dir"] for r in local}


@pytest.mark.parametrize("pair", [("gfsk76800", "g3ruh38400", "g3ruh9600")], indirect=True)
def test_an_adaptive_link_adapts_by_relay(pair):
    """The receiver's mode requests (XID) go by relay too: the sender's I
    frames move to the mode asked for, and each decision is in the log."""
    sender, receiver, _relay, tmp = pair
    got = bytearray()
    receiver.engine.listen(RECEIVER, lambda call: (lambda conn: setattr(conn, "on_data", got.extend)))
    conn = sender.engine.connect(RECEIVER)
    assert _until(lambda: conn.connected, 20)
    data = _payload(30 * 1024)
    conn.send(data)
    assert _until(lambda: len(got) >= len(data) and conn.pending == 0, 120), (len(got), conn.link.adaptation())
    assert bytes(got) == data
    assert conn.link.peer_adapts and conn.link.tx_mode is not None
    heard = {k[6:]: v for k, v in receiver.engine.stats.items() if k.startswith("heard_")}
    assert heard.get(conn.link.tx_mode, 0) > 0, heard
    for side in (sender, receiver):
        side.tele.stop()
    rx = [json.loads(line)["msg"] for line in (tmp / "local-rf-receiver.jsonl").read_text().splitlines()]
    adapts = [m for m in rx if m.get("kind") == "adapt"]
    radio = [m for m in rx if m.get("kind") == "rx-frame" and m["via"] == "radio"]
    assert adapts and {"request", "why", "remote"} <= set(adapts[0])
    # Every frame says how late its receiver found it; the level once the
    # receiver knows its noise floor (None before).
    assert radio and all(m["decode_lag_s"] is not None for m in radio)
    assert any(m["cn0_dbhz"] is not None and m["snr_db"] is not None for m in radio)


def test_losing_the_relay_stops_the_sender_and_says_so(pair):
    sender, receiver, relay, _tmp = pair
    got = bytearray()
    receiver.engine.listen(RECEIVER, lambda call: (lambda conn: setattr(conn, "on_data", got.extend)))
    conn = sender.engine.connect(RECEIVER)
    assert _until(lambda: conn.connected, 20)
    conn.send(_payload(200 * 1024))
    assert _until(lambda: len(got) > 8 * 1024, 60)
    relay.kill()
    assert _until(lambda: conn.state is State.DISCONNECTED, 6)
    assert conn.reason == "return-path-lost"
    lost = [e for e in sender.events if e.kind == "return-path" and e.reason == "lost"]
    assert lost and lost[0].detail["why"] == "relay-closed"
    assert sender.client.state == "lost" and receiver.client.state == "lost"
    # The over under way, if any, ends; then nothing more is keyed.
    assert _until(lambda: not sender.link.keyed, 10)
    pushed = len(sender.pluto.pushed)
    keyed = sender.link.stats["keyed"]
    time.sleep(3)
    assert sender.link.stats["keyed"] == keyed
    assert not [p for p in sender.pluto.pushed[pushed:] if p[1]]
    assert sender.pluto.tx_quiet() and receiver.pluto.tx_quiet()
    with pytest.raises(Exception):
        sender.engine.connect(RECEIVER)


def test_a_relay_that_is_not_there_is_said_plainly(tmp_path):
    c = RelayClient("127.0.0.1", 9, session="nobody-here", role="rf-sender", call=SENDER, peer=RECEIVER)
    with pytest.raises(LinkTestError) as e:
        c.start()
    assert e.value.reason == "unreachable"
    assert parse_address("ohs-box") == ("ohs-box", 30480)
    assert parse_address("ohs-box:30481") == ("ohs-box", 30481)


def test_one_station_per_role(tmp_path):
    relay = RelayThread(tmp_path)
    try:
        a = RelayClient("127.0.0.1", relay.port, session="dup-role", role="rf-sender", call=SENDER, peer=RECEIVER)
        a.start()
        b = RelayClient("127.0.0.1", relay.port, session="dup-role", role="rf-sender", call="G0ABC", peer=RECEIVER)
        with pytest.raises(LinkTestError) as e:
            b.start()
        assert e.value.reason == "refused" and "role-taken" in str(e.value)
        assert a.state == "waiting"
        a.close()
    finally:
        relay.stop()


def test_the_other_station_leaving_drops_the_link_but_keeps_this_one_joined(tmp_path):
    """19/10/2026, GM0OTB: the RF receiver leaving threw the sender out of
    the session too. Now the sender waits, its return path down (so the
    engine drops the link), and is ready again when a receiver rejoins."""
    relay = RelayThread(tmp_path)
    try:
        states, lost = [], []

        class Sink:
            def return_frame(self, path, frame):
                pass

            def return_lost(self, path, reason):
                lost.append(reason)
        a = RelayClient("127.0.0.1", relay.port, session="leave-test", role="rf-sender", call=SENDER, peer=RECEIVER)
        a.on_state = lambda s, why: states.append((s, why))
        a.start()
        path = RelayReturnPath(a, RECEIVER, outbound=False)
        path.bind(Sink())
        b = RelayClient("127.0.0.1", relay.port, session="leave-test", role="rf-receiver", call=RECEIVER,
                        peer=SENDER).start()
        assert _until(lambda: a.ready and b.ready, 3) and path.up
        b.close()
        assert _until(lambda: a.state == "waiting", 3)
        assert states[-1] == ("waiting", "peer-left") and lost == ["peer-left"] and not path.up
        # Still joined: the relay kept the session and its log open.
        time.sleep(0.3)
        assert a.state == "waiting" and "leave-test" in relay.relay.sessions
        b2 = RelayClient("127.0.0.1", relay.port, session="leave-test", role="rf-receiver", call=RECEIVER,
                         peer=SENDER).start()
        assert _until(lambda: a.ready and b2.ready, 3) and path.up
        assert states[-1] == ("ready", "peer-joined") and lost == ["peer-left"]
        b2.close()
        a.close()
        assert a.state == "closed"
    finally:
        relay.stop()
