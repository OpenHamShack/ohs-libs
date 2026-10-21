#!/usr/bin/env python3
"""Two Plutos, one each end, on the fast modes (G3RUH FSK at 9600, 19 200
and 38 400 baud: shack_packet.fsk, radios.pluto with `baud`; and the
OHS-only wide modes, `--mode gfsk|4fsk`: GFSK at 76 800 and 153 600 baud,
4-FSK at 38 400 and 76 800 baud, modem.py). Asked for by Brian on
18/10/2026; every keyed run is his, with him present.

One copy of this runs at each station, with its own Pluto:

- **role A**: shack's Pluto (the Rev.B clone in AD9364 mode), call GM4BRI-1;
- **role B**: the Tezuka nano on workbench (its own ip:192.168.2.1, the
  crystal already corrected at run time), call GM4BRI-2.

The role only sets the default callsigns (`--call`, `--peer`) and the log's
name; everything else is the same code at both ends.

Commands:

    fastfsk_test.py --role A check                       # receive-only: open, TX quiet, 5 s of RX
    fastfsk_test.py --role B listen --seconds 120        # receive-only: frames heard, runs counted
    fastfsk_test.py --role A ui --count 50 --keyed --operator-present
    fastfsk_test.py --role B serve --seconds 300 --keyed --operator-present
    fastfsk_test.py --role A transfer --kb 10 --keyed --operator-present
    fastfsk_test.py --role A --freq 1299.525e6 --mode 4fsk --baud 76800 check
    fastfsk_test.py --role B --freq 1299.525e6 --mode gfsk --baud 76800 --window 63 --paclen 512 --srej \\
        serve --seconds 300 --keyed --operator-present

Modes: `--mode g3ruh` (the default) with `--baud` 9600, 19 200 or 38 400;
`--mode gfsk --baud 76800|153600`; `--mode 4fsk --baud 38400|76800` (baud
is symbols a second: 4-FSK carries two bits each, 76.8 and 153.6 kbit/s).

**A mode wider than 25 kHz keys only inside a band-plan segment that
allows it** (shack_packet.fastsettings.WIDE_DATA_SEGMENTS: the RSGB 2026
plan's high-speed data segments), with the whole occupied bandwidth inside, unless
`--allow-out-of-plan`; and the LO and the IQ image (frequency - ifo, -
2 x ifo) are reported if they fall outside it. A receive-only `check` or
`listen` may be anywhere.

`check` and `listen` never key: the station has transmit off, so the engine
refuses every key and the link opens without its transmit side. `ui` sends
N numbered UI frames (`--per-over` to a transmission) and the other end's
`listen` (or `serve`) counts how many of the run it heard. `transfer`
connects to the peer and sends N KB of seeded random data behind a header
with its length and CRC-32; the peer's `serve` accepts, checks the CRC and
prints the throughput, and so does the sender, from the first byte sent to
the last acknowledged. `serve` keys too: connected mode acknowledges.

**Nothing keys without `--keyed --operator-present`**, and then only under
these caps, on top of PlutoLink's own (the guard, the quiet after every
over, the watchdogs):

- the transmitter guard on the Pluto itself (shack_pluto.guard) is
  required: the link is receive-only if it does not start. Over paramiko
  where the host has it, else OpenSSH through sshpass (workbench has no
  paramiko);
- `--max-key-s` (default 8, at most 10): the link's key-down limit; the
  engine's longest over is 2 s under it;
- `--max-airtime-s` (default 120, at most 600): this station's total time
  on the air for the run; once spent the link refuses to key and the run
  stops;
- `--count` at most 500 frames, `--kb` at most 256;
- `--tx-gain-db` (default -20) no higher than -10 unless Brian says
  `--tx-gain-db` with `--allow-gain-above-minus-10`.

After each over and at the end it prints what to look at: `tx_late` (a push
returned after the DAC should have run dry: a hole in the over),
`tx_gaps` (carrier filled between two pieces of the engine's baseband),
`tx_slack_min_ms` (the least queued ahead), `rx_dropped` (receive fell more
than 2 s behind and lost audio), `rx kept` (receive's samples against the
wall clock, 100 % is real time), the turnaround, and frame success.

Logs go to ~/build/fastfsk-test/<role>-<time>.log on either host.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import random
import shlex
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("fastfsk")

# Run from a checkout (or a copy of one) without installing anything: the
# packages are siblings of this tool.
for src in (HERE.parent / "src", HERE.parent.parent / "shack_pluto" / "src"):
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))

CALLS = {"A": ("GM4BRI-1", "GM4BRI-2"), "B": ("GM4BRI-2", "GM4BRI-1")}
MAX_KEY_CAP_S = 10.0
AIRTIME_CAP_S = 600.0
COUNT_CAP = 500
KB_CAP = 256
GAIN_SOFT_CAP_DB = -10.0
#: AX.25 2.2 negotiates the I field in XID up to 8191 bytes; the FCS's
#: CRC-16 detects every error of up to three bits only up to about 4 KB,
#: and the receiver accepts 2048 (LinkConfig.n1_rx).
PACLEN_CAP = 2048
#: The run's frames say which run they belong to, so a listener counts each
#: run apart and ignores anything else on the channel.
TAG = "FSK"


def load_iio():
    """libiio's bindings: a venv's own, or the system's 0.2x module (Debian's
    python3-libiio), loaded by path so any interpreter can use it."""
    try:
        import iio
        return iio
    except (ImportError, OSError):
        spec = importlib.util.spec_from_file_location("iio", "/usr/lib/python3/dist-packages/iio.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["iio"] = mod
        return mod


# -- the guard's SSH, where there is no paramiko ------------------------------------------

class OpenSshRunner:
    """shack_pluto.guard's Runner over the host's OpenSSH, through sshpass
    for the Pluto's well-known root password (given in the environment, not
    on the command line). The guard script and its protocol are unchanged:
    a closed stdin (this process killed) quiets the transmitter, as with
    paramiko. Host keys are not checked, for SshRunner's reason: a Pluto
    makes a new one every boot."""

    def __init__(self, host: str, password: str = "analog", port: int = 22):
        self.host, self.password, self.port = host, password, port

    def argv(self, command: str) -> list[str]:
        # ConnectTimeout 20: PlutoDVB2's dropbear takes ~6 s to send its
        # banner (shack_pluto.guard.SSH_TIMEOUT_S).
        return ["sshpass", "-e", "ssh", "-T", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=2",
                "-p", str(self.port), f"root@{self.host}", command]

    def start(self, command: str):
        from shack_pluto.guard import _PipeSession
        proc = subprocess.Popen(self.argv(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env={**os.environ, "SSHPASS": self.password})
        return _PipeSession(proc)


def guard_runner(uri: str, ssh_port: int | None = None):
    from shack_pluto.guard import GuardError, SshRunner
    runner = SshRunner.for_uri(uri, ssh_port=ssh_port)
    try:
        import paramiko  # noqa: F401
        return runner
    except ImportError:
        if not shutil_which("sshpass"):
            raise GuardError("guard-no-ssh", "neither paramiko nor sshpass is installed") from None
        return OpenSshRunner(runner.host, port=runner.port)


def shutil_which(name: str):
    import shutil
    return shutil.which(name)


# -- the band plan ------------------------------------------------------------------------

def plan_check(freq: float, mode, ifo_hz: float) -> tuple[str | None, list[str]]:
    """(why this mode may not key here or None, notes on the LO and image):
    shack_packet.fastsettings' UK wide-data check."""
    from shack_packet.fastsettings import wide_data_notes, wide_data_refusal
    refusal = wide_data_refusal(freq, mode)
    return refusal, ([] if refusal else wide_data_notes(freq, mode, ifo_hz))


# -- caps ---------------------------------------------------------------------------------

class Airtime:
    """This station's time on the air, against its cap."""

    def __init__(self, cap_s: float):
        self.cap_s = cap_s
        self.used_s = 0.0
        self.overs = 0
        self.lock = threading.Lock()

    def add(self, seconds: float) -> None:
        with self.lock:
            self.used_s += seconds
            self.overs += 1

    def spent(self) -> bool:
        return self.used_s >= self.cap_s


def check_caps(a) -> None:
    """Refuse a run whose numbers are over the caps (module docstring),
    before anything is opened."""
    if a.max_key_s > MAX_KEY_CAP_S or a.max_key_s < 3:
        raise SystemExit(f"--max-key-s must be 3-{MAX_KEY_CAP_S:g}")
    if a.max_airtime_s > AIRTIME_CAP_S:
        raise SystemExit(f"--max-airtime-s is at most {AIRTIME_CAP_S:g}")
    if getattr(a, "count", 0) > COUNT_CAP:
        raise SystemExit(f"--count is at most {COUNT_CAP}")
    if not 1 <= a.window <= 63:
        raise SystemExit("--window is 1-63 (over 7 is modulo 128; SREJ needs at most 64)")
    if getattr(a, "kb", 0) > KB_CAP:
        raise SystemExit(f"--kb is at most {KB_CAP}")
    if not 1 <= a.paclen <= PACLEN_CAP:
        raise SystemExit(f"--paclen is 1-{PACLEN_CAP}")
    if a.max_tx_gain_db is not None and a.tx_gain_db > a.max_tx_gain_db:
        raise SystemExit(f"--tx-gain-db {a.tx_gain_db:g} is above the amplifier cap --max-tx-gain-db "
                         f"{a.max_tx_gain_db:g}")
    if a.tx_gain_db > GAIN_SOFT_CAP_DB and not a.allow_gain_above_minus_10:
        raise SystemExit(f"--tx-gain-db above {GAIN_SOFT_CAP_DB:g} needs --allow-gain-above-minus-10")


def resolve_mode(a):
    """--mode and --baud to a modem.Mode, or SystemExit naming what exists."""
    from shack_packet import modem
    kind = "g3ruh" if a.mode == "gfsk" and a.baud <= 38_400 else a.mode
    name = f"{kind}{a.baud}"
    if name not in modem.MODES:
        have = ", ".join(f"--mode {m.kind if m.kind != 'g3ruh' else 'g3ruh'} --baud {m.baud}"
                         for m in modem.MODES.values() if m.fast)
        raise SystemExit(f"no {a.mode} mode at {a.baud} baud (have: {have})")
    return modem.MODES[name]


def keyed(a) -> bool:
    return bool(a.keyed and a.operator_present)


def need_keyed(a) -> None:
    if not keyed(a):
        raise SystemExit("this command transmits: it needs --keyed --operator-present, with Brian at the radios")


def capped_link_class():
    from shack_packet.radios import LinkRefused, PlutoLink

    class CappedPlutoLink(PlutoLink):
        """PlutoLink that refuses to key once the run's airtime is spent and
        counts its own airtime, key to unkey."""

        airtime: Airtime | None = None
        _on_air: float | None = None

        def _radio_key(self):
            if self.airtime is not None and self.airtime.spent():
                raise LinkRefused("radio-refused", "the run's airtime cap is spent")
            super()._radio_key()
            self._on_air = time.monotonic()

        def _radio_unkey(self):
            try:
                super()._radio_unkey()
            finally:
                t0, self._on_air = self._on_air, None
                if t0 is not None and self.airtime is not None:
                    self.airtime.add(time.monotonic() - t0)

    return CappedPlutoLink


# -- a station ----------------------------------------------------------------------------

class Station:
    """One end: the Pluto as a fast-mode PlutoLink and the engine on it.
    Receive-only unless `transmit` (keyed and operator present)."""

    def __init__(self, a: argparse.Namespace, transmit: bool):
        self.a = a
        self.transmit = transmit
        self.airtime = Airtime(a.max_airtime_s)
        self.heard: list[str] = []
        self.runs: dict[str, dict] = {}
        self.rx_samples = 0
        self.rx_started = 0.0

    def open(self) -> Station:
        from shack_packet.channel import ChannelConfig
        from shack_packet.datalink import LinkConfig
        from shack_packet.engine import Engine, Runner
        from shack_packet.identity import Station as Ident
        a = self.a
        self.ident = Ident.of(a.call, tx_enabled=self.transmit)
        kw = {}
        if self.transmit:
            from shack_pluto.guard import Guard
            kw["guard"] = Guard(guard_runner(a.uri, a.ssh_port), a.max_key_s,
                                on_line=lambda line: LOG.info("guard on the Pluto: %s", line),
                                gpo=a.ptt_output not in ("none", "firmware"), max_gain_db=a.max_tx_gain_db)
        cls = capped_link_class()
        if a.turnaround_ms is not None:
            kw["turnaround_s"] = a.turnaround_ms / 1000.0
        link_mode = {"modes": a.ladder} if a.ladder else {"mode": a.modem.name}
        self.link = cls(a.uri, frequency=a.freq, **link_mode, tx_gain_db=a.tx_gain_db, rx_gain=a.rx_gain_db,
                        iio_module=load_iio(), station=self.ident, max_key_s=a.max_key_s,
                        transmit=self.transmit, xo_correction=a.xo, ptt_output=a.ptt_output,
                        relay_lead_ms=a.relay_lead_ms, relay_tail_ms=a.relay_tail_ms,
                        max_tx_gain_db=a.max_tx_gain_db, **kw)
        self.link.airtime = self.airtime
        self.link.open()
        LOG.info("%s: Pluto %s open on %.4f MHz, %s at %d baud, %.1f kbit/s (IQ %.4f MS/s, modem %.1f kHz, "
                 "deviation %.0f Hz, occupied %.1f kHz, LO %.0f kHz below, TX filter %.0f kHz), TX %s, guard %s, "
                 "identity %s, xo %s", a.call, a.uri, a.freq / 1e6, a.modem.name, a.modem.baud,
                 a.modem.bitrate / 1e3, self.link.rate_iq / 1e6, self.link.rate / 1e3, self.link.deviation_hz,
                 a.modem.occupied_hz / 1e3, self.link.ifo_hz / 1e3, self.link.tx_bandwidth / 1e3,
                 self.link.tx_state(), self.link.guard_state, self.link.identity, self.link.xo_applied)
        if self.transmit and not self.link.tx_guarded:
            self.link.close()
            raise SystemExit(f"the transmitter guard did not start ({self.link.unguarded_why}); nothing keyed")
        cfg = ChannelConfig(txdelay_s=a.txdelay_ms / 1000.0, txtail_s=0.01, max_tx_s=a.max_key_s - 2.0,
                            fx25=a.fx25, mode=None if a.ladder else a.modem.name, persist=a.persist,
                            slottime_s=a.slottime_ms / 1000.0,
                            max_frames=max(8, a.window + 1))
        # A window over 7 needs modulo 128 (SABME). At 38 400 baud seven
        # 256-byte frames are under half a second of air against a turn
        # round of about a second, so a wider window is most of the speed.
        link = LinkConfig(k=a.window, n1=a.paclen, modulo=128 if a.window > 7 else 8, srej=a.srej,
                          t1_min_s=a.t1_min_s, adapt=a.ladder)
        self.eng = Engine(self.ident, self.link, channel=cfg, link=link)
        self.eng.monitors.append(self._heard)
        self.eng.on_event = lambda e: LOG.info("event: %s %s", e.kind, e.reason)
        engine_saw = self.eng.channel.on_transmit           # the engine's own; kept

        def over(start, end, frames):
            engine_saw(start, end, frames)
            from shack_packet import ax25
            what = [ax25.parse(f) for f in frames[:3]]
            LOG.info("over: %d frame(s), %.3f s on the air: %s%s", len(frames), end - start,
                     "; ".join(_brief(w) for w in what), " ..." if len(frames) > 3 else "")
        self.eng.channel.on_transmit = over
        # Count what reaches the engine, for "rx kept".
        inner = self.eng.pcm_in

        def counted(pcm):
            self.rx_samples += len(pcm) // 2 if isinstance(pcm, (bytes, bytearray)) else len(pcm)
            inner(pcm)
        self.link.sink.pcm_in = counted
        inner_modes = self.eng.mode_in

        def counted_modes(bundle):
            self.rx_samples += len(bundle)
            inner_modes(bundle)
        self.link.sink.mode_in = counted_modes
        self.rx_started = time.monotonic()
        self.runner = Runner(self.eng).start()
        if self.link.rx_listening.wait(10):
            LOG.info("%s: listening on %.4f MHz, %s (receive blocks arriving); TXDELAY sent %.0f ms",
                     a.call, a.freq / 1e6, f"adaptive {' > '.join(a.ladder)}" if a.ladder else a.modem.name,
                     self.eng.channel.txdelay_s * 1000)
        else:
            LOG.error("%s: NOT listening: no receive block in 10 s", a.call)
        return self

    def _heard(self, frame) -> None:
        text = str(frame)
        self.heard.append(text)
        LOG.info("heard: %s", text)
        info = frame.info.decode("latin-1", "replace") if frame.info else ""
        parts = info.split()
        if len(parts) >= 3 and parts[0].startswith(TAG) and "/" in parts[2]:
            run = parts[1]
            i, n = parts[2].split("/")
            r = self.runs.setdefault(run, {"seen": set(), "n": int(n), "from": str(frame.source), "first": time.time()})
            r["seen"].add(int(i))

    def stats(self) -> str:
        st = self.link.stats
        keys = ("keyed", "tx_pushed", "tx_late", "tx_late_ms", "tx_gaps", "tx_fill_s", "tx_slack_min_ms",
                "tx_push_ms_max", "rx_paused", "rx_dropped", "rx_failures", "rx_rebuilds", "rx_max_gap_ms",
                "refused", "watchdog", "radio-error")
        parts = [f"{k}={round(st[k], 3) if isinstance(st[k], float) else st[k]}" for k in keys if k in st]
        elapsed = time.monotonic() - self.rx_started
        kept = self.rx_samples / max(1e-9, elapsed * self.link.rate)
        receivers = [self.eng.receiver] if self.eng.receiver is not None else list(self.eng.receivers.values())
        frames = sum(r.frames for r in receivers)
        fx25 = sum(r.fx25_frames for r in receivers)
        adapting = "".join(f" | adapt {c.remote}: {c.link.adaptation()}" for c in self.eng.connections.values()
                           if c.link.adapter is not None)
        by_mode = {k[6:]: v for k, v in self.eng.stats.items() if k.startswith("heard_")}
        return (" ".join(parts) + f" | rx kept {kept * 100:.1f} % of the time since open (our own overs count as lost) | frames decoded "
                f"{frames} (fx25 {fx25}){' by mode ' + str(by_mode) if by_mode else ''}{adapting} | own echoes dropped {self.eng.stats['own_echo']} | "
                f"airtime {self.airtime.used_s:.1f} s in {self.airtime.overs} over(s) | channel level "
                f"{'-' if self.link.level_db is None else round(self.link.level_db, 1)} dB, last signal "
                f"{getattr(self.link.receiver, 'freq_hz', None)} Hz off | timing "
                f"{self.link.timing()} | TX {self.link.tx_state()}")

    def report_runs(self) -> None:
        for run, r in self.runs.items():
            got = len(r["seen"])
            LOG.info("run %s from %s: heard %d/%d (%.0f %%), missing %s", run, r["from"], got, r["n"],
                     100.0 * got / r["n"], sorted(set(range(1, r["n"] + 1)) - r["seen"])[:40])

    def close(self) -> None:
        try:
            self.runner.stop()
        finally:
            self.link.close()
            LOG.info("closed: TX %s", self.link.tx_state())


def _brief(frame) -> str:
    """A frame for the over log: addresses, type and sequence numbers, and
    a UI frame's text; never a transfer's binary payload."""
    if frame is None:
        return "?"
    c = frame.control
    seq = "".join(f" {k}{v}" for k, v in (("S", c.ns), ("R", c.nr)) if v is not None)
    head = f"{frame.monitor().split(':', 1)[0]} {c.type.name}{seq}"
    if c.type.name == "UI":
        head += " " + frame.info[:40].decode("latin-1", "replace")
    return head


# -- commands -----------------------------------------------------------------------------

def cmd_check(a) -> int:
    """Receive-only: open (the transmitter quieted and read back first),
    take RX for --seconds, report how much came and how loud, close."""
    s = Station(a, transmit=False).open()
    try:
        LOG.info("TX at open: %s; GPO %s", s.link.tx_state(), s.link.gpo_state())
        time.sleep(a.seconds)
        LOG.info("check: %s", s.stats())
        elapsed = time.monotonic() - s.rx_started
        kept = s.rx_samples / (elapsed * s.link.rate)
        ok = s.link.tx_state()["quiet"] and kept > 0.9
    finally:
        s.close()
    ok &= bool(s.link.tx_state().get("quiet"))
    LOG.info("check: %s (rx kept %.1f %% of real time)", "PASS" if ok else "FAIL", kept * 100)
    return 0 if ok else 1


def cmd_listen(a) -> int:
    s = Station(a, transmit=False).open()
    try:
        end = time.monotonic() + a.seconds
        tick = time.monotonic() + 30
        while time.monotonic() < end:
            time.sleep(1)
            if time.monotonic() > tick:
                tick += 30
                LOG.info("listen: %s; %d frame(s) heard; %s",
                         "listening" if s.link.rx_listening.is_set() else "NOT listening", len(s.heard), s.stats())
    except KeyboardInterrupt:
        pass
    finally:
        s.report_runs()
        LOG.info("listen: %d frame(s) heard; %s", len(s.heard), s.stats())
        s.close()
    return 0


def cmd_ui(a) -> int:
    need_keyed(a)
    s = Station(a, transmit=True).open()
    run = time.strftime("%H%M%S")
    sent = 0
    try:
        time.sleep(2.0)                                     # the receiver's floor, for busy
        while sent < a.count and not s.airtime.spent():
            n = min(a.per_over, a.count - sent)
            before = s.eng.channel.stats.transmissions
            for _ in range(n):
                sent += 1
                text = f"{TAG}{a.modem.bitrate} {run} {sent}/{a.count} " + "x" * a.pad
                s.eng.send_ui("APZOHS", text)
            if not wait(lambda: s.eng.channel.stats.transmissions > before and not s.link.keyed, 30):
                LOG.error("ui: the over did not go out (busy channel, refused?); stopping")
                break
            LOG.info("ui: %d/%d sent; %s", sent, a.count, s.stats())
            time.sleep(a.gap_s)
    finally:
        LOG.info("ui: run %s, %d of %d sent, %s. On the other station: its listen counts run %s.",
                 run, sent, a.count, a.modem.name, run)
        LOG.info("ui: %s", s.stats())
        s.close()
    return 0


def payload(kb: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(kb * 1024)


def cmd_transfer(a) -> int:
    need_keyed(a)
    s = Station(a, transmit=True).open()
    data = payload(a.kb)
    header = f"FASTFSK {len(data)} {zlib.crc32(data):08x}\r".encode()
    ok = False
    try:
        time.sleep(2.0)
        conn = s.eng.connect(a.peer)
        t0 = time.monotonic()
        if not wait(lambda: conn.connected, 30):
            LOG.error("transfer: no connection to %s in 30 s (%s)", a.peer, conn.state)
            return 1
        LOG.info("transfer: connected to %s in %.1f s", a.peer, time.monotonic() - t0)
        t1 = time.monotonic()
        conn.send(header + data)
        last = -1
        while conn.pending and conn.connected and not s.airtime.spent():
            time.sleep(0.5)
            if conn.pending != last:
                last = conn.pending
                LOG.info("transfer: %d frame(s) to go; %s", last, s.stats())
        t2 = time.monotonic()
        ok = conn.connected and not conn.pending
        n = len(header) + len(data)
        LOG.info("transfer: %s %d bytes (CRC-32 %08x) in %.1f s: %.0f bit/s of data (%.0f %% of %d bit/s); "
                 "link %s", "sent and acknowledged" if ok else "STOPPED after", n, zlib.crc32(data), t2 - t1,
                 n * 8 / max(1e-9, t2 - t1), 100.0 * n * 8 / max(1e-9, t2 - t1) / a.modem.bitrate, a.modem.bitrate,
                 dict(conn.link.stats))
        from shack_packet.datalink import State
        conn.close()                                # DISC; `connected` goes false at once, so wait for UA
        wait(lambda: conn.state is State.DISCONNECTED, 10)
    finally:
        LOG.info("transfer: %s", s.stats())
        s.close()
    return 0 if ok else 1


def cmd_serve(a) -> int:
    """Accept a transfer (or anything) and check it: keyed, because
    connected mode acknowledges."""
    need_keyed(a)
    s = Station(a, transmit=True).open()
    got = bytearray()
    times: list[float] = []
    result = {}

    def on_data(chunk: bytes):
        if not times:
            times.append(time.monotonic())
        got.extend(chunk)
        if b"\r" in got[:64] and not result:
            head, _, body = bytes(got).partition(b"\r")
            words = head.decode("latin-1").split()
            if len(words) == 3 and words[0] == "FASTFSK" and len(body) >= int(words[1]):
                t = time.monotonic() - times[0]
                crc = zlib.crc32(body[:int(words[1])])
                result.update(ok=f"{crc:08x}" == words[2], n=len(got), t=t)
                LOG.info("serve: %d bytes in %.1f s, %.0f bit/s; CRC-32 %08x, %s", len(got), t,
                         len(got) * 8 / max(t, 1e-9), crc, "MATCHES" if result["ok"] else f"WRONG (sent {words[2]})")

    def accept(call):
        LOG.info("serve: incoming connect from %s", call.remote)

        def setup(conn):
            conn.on_data = on_data
        return setup

    s.eng.listen(a.call, accept)
    try:
        end = time.monotonic() + a.seconds
        while time.monotonic() < end and not s.airtime.spent():
            time.sleep(0.5)
            if a.once and result and not any(c.connected for c in s.eng.connections.values()):
                break                               # one transfer checked and the peer has gone
        # The UA to the peer's DISC is still to go out: closed first, the
        # peer retries its DISC into silence for N2 x T1.
        wait(lambda: not s.eng.channel.pending() and not s.eng.channel.transmitting and not s.link.keyed, 5)
    except KeyboardInterrupt:
        pass
    finally:
        s.report_runs()
        LOG.info("serve: %d bytes received; %s", len(got), s.stats())
        s.close()
    return 0 if result.get("ok") else 1


def wait(pred, timeout: float, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--role", choices=("A", "B"), required=True)
    p.add_argument("--uri", default="ip:192.168.2.1",
                   help="ip:host[:iiodport], or pluto://host?iiod=30431&ssh=30422 (a Pluto shared by the controller)")
    p.add_argument("--ssh-port", type=int, default=None, help="the Pluto's SSH port, for the guard (default 22)")
    p.add_argument("--ptt-output", default="none", choices=("none", "gpo0", "gpo1", "gpo2", "gpo3", "firmware"),
                   help="drive a GPO as PTT for a relay or PA (default none)")
    p.add_argument("--relay-lead-ms", type=float, default=20.0)
    p.add_argument("--relay-tail-ms", type=float, default=10.0)
    p.add_argument("--max-tx-gain-db", type=float, default=None,
                   help="an amplifier is fitted: never drive the Pluto above this (e.g. -20); the guard keeps it")
    p.add_argument("--freq", type=float, default=432_650_000.0, help="Hz (default 432.650 MHz)")
    p.add_argument("--mode", default="g3ruh", choices=("g3ruh", "gfsk", "4fsk"),
                   help="g3ruh (9600-38400), gfsk (76800, 153600) or 4fsk (38400, 76800): OHS-to-OHS only")
    p.add_argument("--baud", type=int, default=9600, choices=(9600, 19_200, 38_400, 76_800, 153_600),
                   help="symbols a second (4fsk carries two bits each)")
    p.add_argument("--adapt", action="store_true",
                   help="link adaptation (adapt.py): each direction steps between --top-mode and --floor-mode; "
                        "both stations must use it; --mode/--baud are then ignored")
    p.add_argument("--top-mode", default="4fsk76800", help="adaptive: the fastest mode (default 4fsk76800)")
    p.add_argument("--floor-mode", default="g3ruh9600",
                   help="adaptive: the slowest, and the one every S and U frame goes at (default g3ruh9600)")
    p.add_argument("--allow-out-of-plan", action="store_true",
                   help="key a mode wider than 25 kHz outside the wide-data segments (Brian's say-so only)")
    p.add_argument("--turnaround-ms", type=float, default=None,
                   help="the Pluto's key-up time the channel allows for (default the link's, 100 ms)")
    p.add_argument("--call", help="default GM4BRI-1 for A, GM4BRI-2 for B")
    p.add_argument("--peer", help="the other station (default the other role's call)")
    p.add_argument("--tx-gain-db", type=float, default=-20.0)
    p.add_argument("--allow-gain-above-minus-10", action="store_true")
    p.add_argument("--rx-gain-db", type=float, default=None, help="manual RX gain (default the AGC)")
    p.add_argument("--xo", type=int, default=None, help="crystal correction to write (default: leave the Pluto's)")
    p.add_argument("--txdelay-ms", type=float, default=200.0)
    p.add_argument("--fx25", default=None, help='None, "auto", 16, 32 or 64')
    p.add_argument("--window", type=int, default=7, choices=range(1, 64), metavar="1-63",
                   help="connected mode k (default 7; over 7 is modulo 128, e.g. 16 or 32 at 38400 baud)")
    p.add_argument("--persist", type=int, default=255,
                   help="CSMA persistence 0-255 (default 255: two stations taking turns answer at once)")
    p.add_argument("--slottime-ms", type=float, default=50.0)
    p.add_argument("--paclen", type=int, default=256, help=f"I field bytes, at most {PACLEN_CAP}")
    p.add_argument("--srej", action="store_true", help="offer selective reject (used if the peer offers it too)")
    p.add_argument("--t1-min-s", type=float, default=1.0, help="the shortest T1 (default 1 s)")
    p.add_argument("--max-key-s", type=float, default=8.0)
    p.add_argument("--max-airtime-s", type=float, default=120.0)
    p.add_argument("--keyed", action="store_true")
    p.add_argument("--operator-present", action="store_true")
    p.add_argument("--log-dir", default=str(Path.home() / "build" / "fastfsk-test"))
    sub = p.add_subparsers(dest="cmd", required=True)
    # The keyed pair is taken either side of the command, since putting it
    # after (`ui --count 50 --keyed`) is the natural way to type it and was
    # refused on the first attempt. SUPPRESS, so a subcommand that is not
    # given them leaves the top level's values alone.
    def keyed_flags(c):
        c.add_argument("--keyed", action="store_true", default=argparse.SUPPRESS)
        c.add_argument("--operator-present", action="store_true", default=argparse.SUPPRESS)

    c = sub.add_parser("check")
    c.add_argument("--seconds", type=float, default=5.0)
    c = sub.add_parser("listen")
    c.add_argument("--seconds", type=float, default=120.0)
    c = sub.add_parser("ui")
    keyed_flags(c)
    c.add_argument("--count", type=int, default=50)
    c.add_argument("--per-over", type=int, default=1)
    c.add_argument("--pad", type=int, default=100, help="bytes of padding in each frame")
    c.add_argument("--gap-s", type=float, default=0.5)
    c = sub.add_parser("transfer")
    keyed_flags(c)
    c.add_argument("--kb", type=int, default=10)
    c = sub.add_parser("serve")
    keyed_flags(c)
    c.add_argument("--seconds", type=float, default=300.0)
    c.add_argument("--once", action="store_true", help="stop after one transfer checked and disconnected")
    return p


COMMANDS = {"check": cmd_check, "listen": cmd_listen, "ui": cmd_ui, "transfer": cmd_transfer, "serve": cmd_serve}


def main(argv=None) -> int:
    a = parser().parse_args(argv)
    mine, theirs = CALLS[a.role]
    a.call = a.call or mine
    a.peer = a.peer or theirs
    if a.fx25 not in (None, "auto"):
        a.fx25 = int(a.fx25)
    check_caps(a)
    a.modem = resolve_mode(a)
    a.ladder = None
    if a.adapt:
        from shack_packet import adapt, modem
        try:
            a.ladder = adapt.ladder(a.top_mode, a.floor_mode)
        except Exception as e:                      # noqa: BLE001 - said, nothing opened
            raise SystemExit(f"--top-mode/--floor-mode: {e}") from None
        a.modem = modem.mode(a.ladder[0])
    if a.cmd in ("ui", "transfer", "serve"):
        from shack_packet.radios.pluto import WIDE_IFO_HZ, WIDE_OCCUPIED_HZ
        ifo = WIDE_IFO_HZ if a.modem.occupied_hz > WIDE_OCCUPIED_HZ else 100_000.0
        if a.ladder:
            from shack_packet import modem
            widest = max((modem.mode(x) for x in a.ladder), key=lambda x: x.occupied_hz)
            ifo = WIDE_IFO_HZ if widest.occupied_hz > WIDE_OCCUPIED_HZ else 100_000.0
            refusal, notes = plan_check(a.freq, widest, ifo)
        else:
            refusal, notes = plan_check(a.freq, a.modem, ifo)
        if refusal and not a.allow_out_of_plan:
            raise SystemExit(f"{refusal}: not keyed (--allow-out-of-plan with Brian's say-so)")
        a.plan_notes = ([refusal] if refusal else []) + notes
    else:
        a.plan_notes = []
    log_dir = Path(a.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{a.role}-{a.cmd}-{a.modem.name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    # Handlers added, not basicConfig: that does nothing once anything has
    # configured logging (a test runner has), and the log file is the record.
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handlers = (logging.FileHandler(path), logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(fmt)
        root.addHandler(handler)
    try:
        LOG.info("fastfsk_test %s; keyed %s; log %s", " ".join(shlex.quote(x) for x in (argv or sys.argv[1:])),
                 keyed(a), path)
        for note in a.plan_notes:
            LOG.warning("band plan: %s", note)
        return COMMANDS[a.cmd](a)
    finally:
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
