#!/usr/bin/env python3
"""The two-station on-air rig for OpenHamShack Packet Radio (agreed with
Brian 18/10/2026).

- **Station A**: the real Windows app (the signed installer testers get) on
  office VM 132 `ohs-packet-a`, driving the IC-9700 over its LAN protocol
  through icom_relay.py on shack's tailnet address. Driven through its own
  window by twostation_win.py (UI Automation), because the app has no
  automation hook and gets none (no servers inside the desktop apps).
- **Station B**: shack's packet engine with the Pluto as its own
  transmitter and receiver (shack_packet.radios.pluto.PlutoLink), with
  pluto_guard.py beside it in a process of its own - or, with
  `--device-guard`, the guard on the Pluto itself (shack_pluto.guard),
  which is what the app uses.

Each station checks the other: B hears A's carrier through the Pluto's
channel power (level_db) and counts A's airtime from it; A's window shows
what A heard and whether B acknowledged.

**Nothing keys without `--keyed --operator-present`.** Without both, station
B's Station has transmit off (the engine refuses every key) and station A is
configured with "this computer may transmit" off, so its link opens receive
only and Arm is refused by the app itself - and the harness never clicks
Arm. With both, two caps apply on top of each station's own limits:

- `--max-key-s` (default 8): B's PlutoLink max_key_s (its watchdog and the
  guard's follow from it; the engine's longest over is kept 2 s under it).
  A's longest over is the app's own (15 s); if B hears A's carrier for
  longer than `--max-key-a-s` (default 17) the harness kills the app and,
  if the carrier is still there, unkeys the IC-9700 directly over CI-V.
- `--max-airtime-s` (default 240): A's and B's airtime together. When it is
  spent B refuses to key, A is disarmed and the run stops.

Commands (in the order Brian runs them):

    twostation.py install-a OpenHamShack-Packet-Radio-X-windows-x64.exe   # swap station A's build
    twostation.py check                    # unkeyed: relay, VM, app, Pluto quiet + receiving
    twostation.py rx-verify                # unkeyed: A logs in through the relay, reads freq, RX audio
    twostation.py listen --seconds 120     # unkeyed: B receive-only, prints frames and A's carriers
    twostation.py run --keyed --operator-present [--only aprs-ab,...]
    twostation.py stuck --keyed --operator-present [--only a-killed,b-killed]
    twostation.py stuck --keyed --operator-present --device-guard --only b-killed,b-frozen   # R3

Scenarios (`run`): aprs-ab, aprs-ba (APRS message both ways, acked);
connect-ab, chat-ab, disconnect-ab (A connects, 10 lines A->B echoed, A
disconnects); connect-ba, chat-ba, transfer-ba, disconnect-ba (B connects,
10 lines B->A, 2 KB B->A, B disconnects). 20 chat lines in all.

Stuck-TX (`stuck`): a-killed - the app is killed (taskkill /F) mid-over and
B times how long the IC-9700's carrier lasts after; then the radio's PTT is
read over CI-V (1C 00) from shack once the radio lets a client in again.
b-killed - a child process keys the Pluto and is killed (SIGKILL) mid-over;
the guard must quiet it, read back by a context of the harness's own.

Settings: --freq (default 432.650 MHz), --a-call GM4BRI-1, --b-call
GM4BRI-2, --pluto-tx-gain-db (default -10), --pluto-rx-gain-db (default
25, manual: the AGC overloads with the 9700 nearby), --radio-json
(~/.config/ohs-twostation/ic9700.json: RADIO_USER, RADIO_PASSWORD).
Writes a log to ~/build/ohs-twostation/run-<time>.log.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("twostation")

VM_HOST = "192.0.2.41"
VM_JUMP = "office"
VM_USER = "labadmin"
RELAY = "shack"
RADIO_LAN = "198.51.100.150"
PLUTO_URI = "ip:192.168.2.1"
RIG_DIR = r"C:\ohs\rig"
AGENT_PORT = 47001
#: One SSH connection kept open for the PowerShell calls (each new one costs
#: two handshakes, through office).
SSH_MUX = ["-o", "ControlMaster=auto", "-o", f"ControlPath={Path.home()}/.ssh/cm-ohs-twostation-%C",
           "-o", "ControlPersist=120"]                  # twostation_win.py, on the VM's loopback

#: B hears A's carrier when the Pluto's channel is this far over its floor.
A_CARRIER_DB = 12.0
#: B's own over is still on its receiver this long after it unkeys.
B_TAIL_S = 0.5


def load_iio():
    try:
        import iio
        return iio
    except ImportError:
        spec = importlib.util.spec_from_file_location("iio", "/usr/lib/python3/dist-packages/iio.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["iio"] = mod
        return mod


# -- airtime ------------------------------------------------------------------------------

class Airtime:
    """Both stations' time on the air, and the caps. B's is exact (its own
    key and unkey); A's is what B heard of A's carrier."""

    def __init__(self, total_s: float, max_key_a_s: float):
        self.total_s = total_s
        self.max_key_a_s = max_key_a_s
        self.a_s = 0.0
        self.b_s = 0.0
        self.a_overs: list[float] = []
        self.b_overs: list[float] = []
        self.lock = threading.Lock()

    def add(self, who: str, seconds: float) -> None:
        with self.lock:
            if who == "a":
                self.a_s += seconds
                self.a_overs.append(round(seconds, 2))
            else:
                self.b_s += seconds
                self.b_overs.append(round(seconds, 2))

    def used(self) -> float:
        return self.a_s + self.b_s

    def spent(self) -> bool:
        return self.used() >= self.total_s

    def summary(self) -> str:
        return (f"airtime A {self.a_s:.1f} s in {len(self.a_overs)} over(s), longest "
                f"{max(self.a_overs, default=0):.1f} s; B {self.b_s:.1f} s in {len(self.b_overs)}, longest "
                f"{max(self.b_overs, default=0):.1f} s; total {self.used():.1f} of {self.total_s:.0f} s")


class CarrierWatch:
    """A's carrier as B hears it. `feed(level_db, b_busy_tx, now)` once per
    sample; keyed is B's own transmitter (or its tail), when A cannot be
    told apart from B and nothing is counted."""

    def __init__(self, airtime: Airtime, threshold_db: float = A_CARRIER_DB, on_overlong=None):
        self.airtime = airtime
        self.threshold_db = threshold_db
        self.on_overlong = on_overlong
        self.since: float | None = None
        self.last_on: float | None = None
        self.last_off: float | None = None
        self.overlong_said = False
        self.edges: list[tuple[float, bool]] = []

    @property
    def on(self) -> bool:
        return self.since is not None

    def feed(self, level_db: float | None, b_keyed: bool, now: float) -> None:
        if b_keyed:
            if self.since is not None:
                self._end(now)
            return
        carrier = level_db is not None and level_db > self.threshold_db
        if carrier and self.since is None:
            self.since = now
            self.overlong_said = False
            self.edges.append((now, True))
        elif not carrier and self.since is not None:
            self._end(now)
        if carrier:
            self.last_on = now
            if now - self.since > self.airtime.max_key_a_s and not self.overlong_said:
                self.overlong_said = True
                if self.on_overlong is not None:
                    self.on_overlong(now - self.since)

    def _end(self, now: float) -> None:
        self.airtime.add("a", now - self.since)
        self.since = None
        self.last_off = now
        self.edges.append((now, False))


# -- station A: the Windows VM -----------------------------------------------------------

_OPEN: list = []


class WinA:
    """Station A over SSH: PowerShell without quoting trouble (an encoded
    command), files by base64 on stdin, and the window through
    twostation_win.py's file queue in the desktop session."""

    def __init__(self, host=VM_HOST, jump=VM_JUMP, user=VM_USER, port=47101):
        self.target = f"{user}@{host}"
        self.jump = jump
        self.port = port            # shack's end of the tunnel
        self._tun: subprocess.Popen | None = None
        _OPEN.append(self)

    def ps(self, script: str, stdin: bytes | None = None, timeout: float = 120) -> str:
        script = "$ProgressPreference='SilentlyContinue'\n" + script
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        p = subprocess.run(["ssh", "-o", "ConnectTimeout=15", *SSH_MUX, "-J", self.jump, self.target,
                            f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"],
                           input=stdin, capture_output=True, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace")
        # PowerShell's progress records arrive as CLIXML on stdout over SSH.
        return "\n".join(line for line in out.splitlines()
                         if not line.startswith(("#< CLIXML", "<Objs ")))

    @staticmethod
    def _sftp_path(win_path: str) -> str:
        return "/" + win_path.replace("\\", "/")

    def put(self, data: bytes, dest: str) -> None:
        """A file to the VM by scp (SFTP). Not through PowerShell's stdin:
        anything over a few KB piped that way hangs on Windows OpenSSH."""
        import tempfile
        self.ps(f'New-Item -ItemType Directory -Force (Split-Path "{dest}") | Out-Null')
        with tempfile.NamedTemporaryFile(dir=Path.home() / "build", delete=True) as f:
            f.write(data)
            f.flush()
            subprocess.run(["scp", "-q", *SSH_MUX, "-J", self.jump, f.name, f"{self.target}:{self._sftp_path(dest)}"],
                           check=True, timeout=120)

    def put_tree(self, src: Path, dest: str) -> None:
        self.ps(f'Remove-Item -Recurse -Force "{dest}" -ErrorAction SilentlyContinue; '
                f'New-Item -ItemType Directory -Force (Split-Path "{dest}") | Out-Null')
        subprocess.run(["scp", "-q", "-r", *SSH_MUX, "-J", self.jump, str(src),
                        f"{self.target}:{self._sftp_path(dest)}"],
                       check=True, timeout=300)

    def start_agent(self) -> None:
        """twostation_win.py in labadmin's desktop session (a scheduled task
        with /IT: an SSH session cannot see the desktop's windows)."""
        self.put((HERE / "twostation_win.py").read_bytes(), RIG_DIR + r"\twostation_win.py")
        self.put(b"@echo off\r\npython C:\\ohs\\rig\\twostation_win.py serve > C:\\ohs\\rig\\serve.log 2>&1\r\n",
                 RIG_DIR + r"\serve.cmd")
        self.ps(r"""
$p = Get-Content C:\ohs\rig\serve.pid -ErrorAction SilentlyContinue
if ($p -and (Get-Process -Id $p -ErrorAction SilentlyContinue)) { Stop-Process -Id $p -Force }
schtasks /create /tn ohs-rig /tr C:\ohs\rig\serve.cmd /sc once /st 23:59 /ru labadmin /it /f | Out-Null
schtasks /run /tn ohs-rig | Out-Null""")
        end = time.time() + 30
        while time.time() < end:
            if self.call("ping", timeout=5).get("ok"):
                return
            time.sleep(1)
        raise RuntimeError("station A's window driver did not start (C:\\ohs\\rig\\serve.log)")

    def _tunnel(self) -> None:
        if self._tun is not None and self._tun.poll() is None:
            return
        self._tun = subprocess.Popen(["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=10",
                                      "-L", f"127.0.0.1:{self.port}:127.0.0.1:{AGENT_PORT}", "-J", self.jump,
                                      self.target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        end = time.time() + 30
        import socket
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", self.port), 1).close()
                return
            except OSError:
                if self._tun.poll() is not None:
                    raise RuntimeError("the SSH tunnel to station A's window driver failed")
                time.sleep(0.3)

    def call(self, op: str, timeout: float = 60, **args) -> dict:
        """One request to twostation_win.py, through the tunnel: a few tens of
        milliseconds, where a PowerShell round trip over SSH is seconds."""
        import socket
        self._tunnel()
        cid = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        try:
            with socket.create_connection(("127.0.0.1", self.port), 10) as sk:
                sk.settimeout(timeout)
                sk.sendall((json.dumps({"op": op, "id": cid, **args}) + "\n").encode())
                with sk.makefile("rb") as f:
                    line = f.readline()
        except OSError as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return json.loads(line) if line else {"ok": False, "error": "no answer"}

    def close(self) -> None:
        if self._tun is not None:
            self._tun.terminate()
            self._tun.wait(5)
            self._tun = None

    def configure(self, *, call: str, ssid: int, freq: int, transmit: bool, conf: dict) -> None:
        """Station A's settings.json and secrets.json, as the app would save
        them: the IC-9700 over the relay, minimum power for test on, and
        'this computer may transmit' only when keyed."""
        settings = {
            "schema": 1,
            "station": {"callsign": call, "ssid": ssid, "locator": "IO75RL", "country": "GB",
                        "licence_class": "full"},
            "radio": {"kind": "ic-9700", "transport": "lan", "host": RELAY, "port": 50001,
                      "username": conf["RADIO_USER"], "minimum_power_for_test": True},
            "channel": {"preset": "custom", "frequency_hz": int(freq), "fx25": None},
            "chat": {"accept_incoming": True},
            "tx": {"set_up": bool(transmit), "arm_minutes": 10},
            "setup_done": True,
        }
        base = r"$env:APPDATA\openhamshack-packet"
        self.ps(f'New-Item -ItemType Directory -Force "{base}" | Out-Null; '
                f'[IO.File]::WriteAllText("{base}\\settings.json", [Console]::In.ReadToEnd())',
                stdin=json.dumps(settings, indent=1).encode())
        self.ps(f'[IO.File]::WriteAllText("{base}\\secrets.json", [Console]::In.ReadToEnd())',
                stdin=json.dumps({"icom_password": conf["RADIO_PASSWORD"]}).encode())

    def kill_app_now(self) -> float:
        """taskkill /F by the window driver (a process apart from the app, so
        the stuck-TX test does not depend on the thing it tests), and over a
        plain SSH command if the driver does not answer."""
        t = time.time()
        if not self.call("kill_app", timeout=10).get("ok"):
            self.ps("taskkill /F /IM shack-packet.exe | Out-Null", timeout=30)
        return t

    def log_tail(self, n: int = 40) -> list[str]:
        return self.call("log_tail", n=n).get("lines", [])


# -- station B: shack's engine and the Pluto ---------------------------------------------

def b_guard(a) -> dict:
    """PlutoLink's guard arguments for station B. By default the harness
    keeps what it was proved with: pluto_guard.py beside it on shack, and
    the link told it may key without a guard on the Pluto. --device-guard
    gives B the on-device guard (shack_pluto.guard) instead, which is what
    the app uses and what session R3 proves; the host guard is then not
    started, so the on-device one is the only thing outside the process."""
    if not getattr(a, "device_guard", False):
        return {"allow_unguarded": True}
    from shack_pluto.guard import Guard, SshRunner
    return {"guard": Guard(SshRunner.for_uri(PLUTO_URI), a.max_key_s,
                           on_line=lambda line: LOG.info("B guard on the Pluto: %s", line))}


def capped_link_class():
    from shack_packet.radios import LinkRefused, PlutoLink

    class CappedPlutoLink(PlutoLink):
        """PlutoLink that refuses to key once the run's airtime is spent and
        counts its own airtime."""

        airtime: Airtime | None = None

        def _radio_key(self):
            if self.airtime is not None and self.airtime.spent():
                raise LinkRefused("radio-refused", "the run's airtime cap is spent")
            super()._radio_key()
            self._on_air = time.monotonic()

        def _radio_unkey(self):
            try:
                super()._radio_unkey()
            finally:
                t0, self._on_air = getattr(self, "_on_air", None), None
                if t0 is not None and self.airtime is not None:
                    self.airtime.add("b", time.monotonic() - t0)
                    self.unkeyed_at = time.monotonic()

    return CappedPlutoLink


class StationB:
    def __init__(self, a: argparse.Namespace, airtime: Airtime, keyed: bool):
        self.a = a
        self.airtime = airtime
        self.keyed = keyed
        self.heard: list[str] = []
        self.messages: list = []
        self.lines: dict[str, list[str]] = {}
        self.guard: subprocess.Popen | None = None
        self.stop = threading.Event()
        self.watch = CarrierWatch(airtime, on_overlong=self._a_overlong)
        self.on_a_overlong = None

    def open(self):
        from shack_packet.aprs_station import AprsStation
        from shack_packet.channel import ChannelConfig
        from shack_packet.engine import Engine, Runner
        from shack_packet.identity import Station
        iio = load_iio()
        cls = capped_link_class()
        self.station = Station.of(self.a.b_call, tx_enabled=self.keyed)
        self.link = cls(PLUTO_URI, frequency=self.a.freq, tx_gain_db=self.a.pluto_tx_gain_db,
                        rx_gain=self.a.pluto_rx_gain_db, iio_module=iio, station=self.station,
                        max_key_s=self.a.max_key_s, **b_guard(self.a))
        self.link.airtime = self.airtime
        self.link.unkeyed_at = 0.0
        self.link.open()
        LOG.info("B: Pluto open on %.4f MHz, TX %s", self.a.freq / 1e6, self.link.tx_state())
        if not getattr(self.a, "device_guard", False):
            self.guard = subprocess.Popen([sys.executable, str(HERE / "pluto_guard.py"), "--uri", PLUTO_URI,
                                           "--pid", str(os.getpid()), "--max-key-s", str(self.a.max_key_s)])
        cfg = ChannelConfig(max_tx_s=max(2.0, self.a.max_key_s - 2.0))
        self.eng = Engine(self.station, self.link, channel=cfg)
        self.eng.monitors.append(lambda f: self._heard(f))
        self.eng.on_event = lambda e: LOG.info("B event: %s %s", e.kind, e.reason)
        self.aprs = AprsStation(self.eng)
        self.aprs.on_message = lambda m: (self.messages.append(m),
                                          LOG.info("B: APRS message from %s: %r", m.source, m.text))
        self.eng.listen(self.a.b_call, lambda call: (LOG.info("B: incoming connect from %s", call.remote), True)[1])
        self.runner = Runner(self.eng).start()
        self.watcher = threading.Thread(target=self._watch, name="carrier watch", daemon=True)
        self.watcher.start()
        self.echoer = threading.Thread(target=self._echo, name="echo", daemon=True)
        self.echoer.start()
        return self

    def _heard(self, f):
        text = str(f)
        self.heard.append(text)
        LOG.info("B heard: %s", text)

    def _watch(self):
        while not self.stop.is_set():
            now = time.monotonic()
            b_keyed = self.link.keyed or now - getattr(self.link, "unkeyed_at", 0.0) < B_TAIL_S
            self.watch.feed(self.link.level_db, b_keyed, now)
            time.sleep(0.05)

    def _a_overlong(self, seconds: float):
        LOG.error("B: A's carrier has been up %.1f s, past --max-key-a-s", seconds)
        if self.on_a_overlong is not None:
            threading.Thread(target=self.on_a_overlong, args=(seconds,), daemon=True).start()

    def _echo(self):
        """Every connected-mode line B receives is echoed back, as the
        morning's listen.py did with Ray: A's screen then shows both ends."""
        while not self.stop.is_set():
            for key, conn in list(self.eng.connections.items()):
                if conn.connected:
                    got = conn.read()
                    if got:
                        text = got.decode("ascii", "replace")
                        for line in text.replace("\n", "\r").split("\r"):
                            if line.strip():
                                self.lines.setdefault(str(conn.remote), []).append(line.strip())
                                if getattr(self, "echo", True):
                                    conn.send(f"B got: {line.strip()}\r")
            time.sleep(0.2)

    def connection_with(self, call: str):
        for conn in list(self.eng.connections.values()):
            if str(conn.remote).upper() == call.upper():
                return conn
        return None

    def close(self):
        self.stop.set()
        try:
            for conn in list(self.eng.connections.values()):
                if conn.connected:
                    conn.close()
            time.sleep(1)
            self.runner.stop()
        except Exception:                           # noqa: BLE001 - closing regardless
            LOG.exception("B: stopping the engine")
        finally:
            self.link.close()
            LOG.info("B: Pluto closed, TX %s", self.link.tx_state())
            if self.guard is not None:
                self.guard.terminate()
                self.guard.wait(5)


def read_pluto_tx() -> tuple[str, str]:
    """The Pluto's TX as a fresh context of our own reads it."""
    iio = load_iio()
    ctx = iio.Context(PLUTO_URI)
    phy = ctx.find_device("ad9361-phy")
    return (phy.find_channel("altvoltage1", True).attrs["powerdown"].value.strip(),
            phy.find_channel("voltage0", True).attrs["hardwaregain"].value.strip())


# -- the IC-9700 from shack, for the stuck-TX checks ----------------------------------------

def icom_direct(conf: dict, unkey: bool, deadline_s: float = 60) -> dict:
    """Log in to the IC-9700 straight over shack's LAN (not the relay) as
    soon as it lets a client in, read PTT (1C 00) and, if `unkey`, send
    PTT off - the one CI-V write that may always be sent - and read again."""
    import asyncio
    from shack_rig.icom import make_rig

    async def go():
        profile = {"model": "ic-9700", "transport": "lan", "host": RADIO_LAN, "port": 50001,
                   "username": conf["RADIO_USER"], "audio_rate": 0}
        end = time.time() + deadline_s
        tries = 0
        while time.time() < end:
            tries += 1
            rig = make_rig(profile, password=conf["RADIO_PASSWORD"], tx_enabled=False, poll_interval=0.5,
                           scope=False, client_name="shack-packet-stuckcheck")
            await rig.start()
            try:
                t = time.time()
                while time.time() - t < 8 and not (rig.state.get("connected") and rig.state.get("freq")):
                    await asyncio.sleep(0.1)
                if not rig.state.get("connected"):
                    continue
                out = {"tries": tries, "at": time.time()}
                g = await rig.ask(b"\x1c\x00", b"\x1c\x00", 1.0)
                out["ptt"] = g.hex(" ") if g else None
                if unkey:
                    out["unkey_sent"] = rig.set_ptt(False)
                    await asyncio.sleep(0.3)
                    g = await rig.ask(b"\x1c\x00", b"\x1c\x00", 1.0)
                    out["ptt_after"] = g.hex(" ") if g else None
                pw = await rig.ask(b"\x14\x0a", b"\x14\x0a", 1.0)
                out["rf_power"] = pw.hex(" ") if pw else None
                return out
            finally:
                await rig.stop()
            await asyncio.sleep(2)
        return {"error": f"the radio did not let a client in within {deadline_s:.0f} s", "tries": tries}

    return asyncio.run(go())


def notify(message: str) -> None:
    path = Path.home() / ".config" / "claude-account" / "notify"
    if path.exists():
        subprocess.run([str(path), message], timeout=20, capture_output=True)


# -- commands ------------------------------------------------------------------------------

def need_keyed(a) -> None:
    if not (a.keyed and a.operator_present):
        raise SystemExit("this command transmits: it needs --keyed --operator-present, with Brian at the "
                         "radios and the IC-9700 into its dummy load")


def wait(pred, timeout: float, step: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def cmd_check(a, conf) -> int:
    """Unkeyed preflight. Nothing here transmits: the Pluto is opened with no
    station (key() is refused) and A is not touched beyond reading."""
    ok = True
    r = subprocess.run(["systemctl", "--user", "is-active", "ohs-icom-relay"], capture_output=True, text=True)
    LOG.info("relay service: %s", r.stdout.strip())
    ok &= r.stdout.strip() == "active"
    win = WinA()
    out = win.ps(r"""
$d="$env:LOCALAPPDATA\Programs\OpenHamShack\Packet Radio"
'version ' + (& "$d\shack-packet-console.exe" --version)
'tailscale ' + (& "C:\Program Files\Tailscale\tailscale.exe" ip -4 2>&1)
'running ' + ((Get-Process shack-packet -ErrorAction SilentlyContinue | Measure-Object).Count)""")
    LOG.info("station A: %s", " | ".join(out.split("\n")))
    ok &= "version" in out
    # A reaches the relay only over the tailnet.
    ts = next((line.split()[1] for line in out.split("\n") if line.startswith("tailscale ") and
               line.split()[1:2] and line.split()[1].startswith("100.")), None)
    if ts is None:
        LOG.error("station A is not on the tailnet: approve its Tailscale login")
    ok &= ts is not None
    from shack_packet.radios import PlutoLink
    link = PlutoLink(PLUTO_URI, frequency=a.freq, rx_gain=a.pluto_rx_gain_db, iio_module=load_iio(),
                     allow_unguarded=True)             # never keyed here
    audio = []

    class Sink:
        station = None

        def pcm_in(self, pcm):
            audio.append(len(pcm) // 2)

        def frame_in(self, f):
            pass

        def busy_in(self, b):
            pass

        def link_event(self, *e):
            LOG.info("B event %s", e)

    link.attach(Sink())
    link.open()
    try:
        LOG.info("B: Pluto TX at open %s", link.tx_state())
        time.sleep(5)
        LOG.info("B: %.2f s of RX audio in 5 s, channel %s dB over its floor, TX %s",
                 sum(audio) / 12000, None if link.level_db is None else round(link.level_db, 1), link.tx_state())
        ok &= link.tx_state()["quiet"] and sum(audio) / 12000 > 4.0
    finally:
        link.close()
    pd, gain = read_pluto_tx()
    LOG.info("B: Pluto TX after close, fresh read: powerdown %s, %s", pd, gain)
    ok &= pd == "1"
    LOG.info("check: %s", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_rx_verify(a, conf) -> int:
    """Station A logs in to the IC-9700 through the relay, receive-only, two
    ways: icom_rxcheck.py from a copy of shack_rig on the VM, then the app
    itself with transmit not set up (its link opens receive-only)."""
    win = WinA()
    src = HERE.parent.parent / "shack_rig" / "src" / "shack_rig"
    LOG.info("copying shack_rig to the VM")
    win.put_tree(src, RIG_DIR + r"\pylib\shack_rig")
    win.put((HERE / "icom_rxcheck.py").read_bytes(), RIG_DIR + r"\icom_rxcheck.py")
    win.put(json.dumps(conf).encode(), RIG_DIR + r"\radio.json")
    out = win.ps(rf"""$env:PYTHONPATH="C:\ohs\rig\pylib"; python C:\ohs\rig\icom_rxcheck.py C:\ohs\rig\radio.json {RELAY} 8; "exit $LASTEXITCODE"
Remove-Item C:\ohs\rig\radio.json""", timeout=90)
    LOG.info("A (icom_rxcheck through the relay): %s", out.strip())
    ok = "exit 0" in out
    win.configure(call=a.a_call.split("-")[0], ssid=int(a.a_call.split("-")[1]), freq=a.freq, transmit=False,
                  conf=conf)
    win.start_agent()
    r = win.call("start_app", timeout=60)
    LOG.info("A: app %s", r)
    connected = wait(lambda: any(t.endswith(": Connected") for t in win.call("status").get("bar", [])), 40, 2)
    st = win.call("status")
    LOG.info("A: station bar %s", st.get("bar"))
    time.sleep(10)
    LOG.info("A: log tail:\n  %s", "\n  ".join(win.log_tail(25)))
    LOG.info("A: closing the app: %s", win.call("close_app"))
    ok &= connected
    LOG.info("rx-verify: %s", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_listen(a, conf) -> int:
    """Station B receive-only for --seconds: the engine on the Pluto with
    transmit off, printing what it hears and A's carriers. Keys nothing."""
    airtime = Airtime(a.max_airtime_s, a.max_key_a_s)
    b = StationB(a, airtime, keyed=False).open()
    try:
        time.sleep(a.seconds)
    finally:
        b.close()
    LOG.info("heard %d frame(s); A's carriers %s; %s", len(b.heard), airtime.a_overs, airtime.summary())
    return 0


def cmd_install_a(a, conf) -> int:
    """Put a Packet Radio installer on station A and install it silently, per
    user as a tester would (Inno Setup /VERYSILENT), then show the version,
    the self-test and the signature."""
    exe = Path(a.installer)
    win = WinA()
    dest = RIG_DIR + "\\" + exe.name
    LOG.info("copying %s (%d bytes) to A", exe.name, exe.stat().st_size)
    win.put(exe.read_bytes(), dest)
    out = win.ps(rf"""
Get-Process shack-packet -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Process "{dest}" -Wait -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/LOG=C:\ohs\rig\install.log'
$d="$env:LOCALAPPDATA\Programs\OpenHamShack\Packet Radio"
'version ' + (& "$d\shack-packet-console.exe" --version)
& "$d\shack-packet-console.exe" --self-test 2>&1 | Select-Object -Last 1
'signature ' + (Get-AuthenticodeSignature "$d\shack-packet.exe").Status + ' ' + (Get-AuthenticodeSignature "$d\shack-packet.exe").SignerCertificate.Subject
Remove-Item "{dest}" """, timeout=600)
    LOG.info("A: %s", " | ".join(x for x in out.split("\n") if x.strip()))
    return 0 if "self-test: PASS" in out else 1


SCENARIOS = ["aprs-ab", "aprs-ba", "connect-ab", "chat-ab", "disconnect-ab",
             "connect-ba", "chat-ba", "transfer-ba", "disconnect-ba"]


def cmd_run(a, conf) -> int:
    need_keyed(a)
    airtime = Airtime(a.max_airtime_s, a.max_key_a_s)
    win = WinA()
    results: dict[str, str] = {}
    win.configure(call=a.a_call.split("-")[0], ssid=int(a.a_call.split("-")[1]), freq=a.freq, transmit=True,
                  conf=conf)
    win.start_agent()
    b = StationB(a, airtime, keyed=True).open()
    b.on_a_overlong = lambda s: emergency_a(win, b, conf, s)
    try:
        LOG.info("A: %s", win.call("start_app", timeout=60))
        if not wait(lambda: any(t.endswith(": Connected") for t in win.call("status").get("bar", [])), 40, 2):
            raise RuntimeError(f"A's radio did not connect: {win.call('status')}")
        armed = win.call("arm", keyed=True)
        LOG.info("A: arm %s", armed)
        if not armed.get("armed"):
            raise RuntimeError(f"A would not arm: {armed}")
        only = a.only.split(",") if a.only else SCENARIOS
        for name in SCENARIOS:
            if name not in only:
                continue
            if airtime.spent():
                results[name] = "SKIPPED (airtime cap)"
                continue
            LOG.info("=== %s (%s)", name, airtime.summary())
            try:
                ok, detail = SCENARIO_FUNCS[name](a, win, b)
            except Exception as e:                  # noqa: BLE001 - next scenario
                LOG.exception("%s", name)
                ok, detail = False, f"{type(e).__name__}: {e}"
            results[name] = ("PASS " if ok else "FAIL ") + detail
            LOG.info("%s: %s", name, results[name])
    finally:
        LOG.info("A: disarm %s", win.call("disarm"))
        b.close()
        LOG.info("A: close %s", win.call("close_app"))
        LOG.info("A: log tail:\n  %s", "\n  ".join(win.log_tail(30)))
    LOG.info("RESULTS\n  %s\n  %s", "\n  ".join(f"{k}: {v}" for k, v in results.items()), airtime.summary())
    return 0 if all(v.startswith(("PASS", "SKIP")) for v in results.values()) else 1


def emergency_a(win: WinA, b: StationB, conf: dict, seconds: float) -> None:
    """A's carrier has outlasted its limit: kill the app, then if B still
    hears the carrier, unkey the IC-9700 directly. Brian is told either way."""
    LOG.error("EMERGENCY: A keyed %.1f s; killing the app", seconds)
    notify(f"Packet rig: station A keyed {seconds:.0f}s - killing the app")
    win.kill_app_now()
    if wait(lambda: not b.watch.on, 3, 0.1):
        LOG.error("EMERGENCY: carrier gone after the app was killed")
        return
    LOG.error("EMERGENCY: carrier still up after the kill: unkeying the IC-9700 over CI-V from shack")
    r = icom_direct(conf, unkey=True, deadline_s=30)
    LOG.error("EMERGENCY: direct unkey %s; carrier now %s", r, "ON" if b.watch.on else "off")
    if b.watch.on:
        notify("Packet rig: IC-9700 STILL KEYED - unkey it at the radio")


# -- scenarios: each returns (ok, detail) ------------------------------------------------------

def _stamp() -> str:
    return time.strftime("%H%M%S")


def s_aprs_ab(a, win, b):
    text = f"A to B {_stamp()}"
    n0 = len(b.messages)
    LOG.info("A: send %s", win.call("send_message", to=a.b_call, text=text))
    if not wait(lambda: any(m.text == text for m in b.messages[n0:]), 90):
        return False, "B never heard A's message"
    acked = wait(lambda: any(text in r[2] and r[3] == "Acknowledged"
                             for r in win.call("messages").get("rows", [])), 90, 3)
    return acked, "B heard it; A's row " + ("Acknowledged" if acked else "never acknowledged")


def s_aprs_ba(a, win, b):
    text = f"B to A {_stamp()}"
    msgs = b.aprs.send_message(a.a_call, text)
    if not wait(lambda: all(m.state == "acked" for m in msgs), 120):
        return False, f"B's message states {[m.state for m in msgs]}"
    win.call("select_peer", peer=a.b_call)
    seen = any(text in r[2] for r in win.call("messages").get("rows", []))
    return seen, "acked by A; " + ("on A's screen" if seen else "NOT on A's Messages page")


def s_connect_ab(a, win, b):
    LOG.info("A: %s", win.call("connect", call=a.b_call))
    if not wait(lambda: (c := b.connection_with(a.a_call)) is not None and c.connected, 60):
        return False, "B never saw the connection"
    ok = wait(lambda: any(f"Connected to {a.b_call}" in t for t in win.call("chat_state").get("labels", [])), 30, 2)
    return ok, "connected both ends" if ok else "B connected, A's window does not say so"


def s_chat_ab(a, win, b):
    lines = [f"A line {i + 1:02d} {_stamp()}" for i in range(10)]
    got0 = len(b.lines.get(a.a_call, []))
    win.call("chat_send", lines=lines, gap_s=0.3, timeout=120)
    if not wait(lambda: len(b.lines.get(a.a_call, [])) - got0 >= 10, 180, 1):
        return False, f"B got {len(b.lines.get(a.a_call, [])) - got0} of 10"
    screen = "\n".join(win.call("chat_screen").get("screens", []))
    echoed = wait(lambda: f"B got: {lines[-1]}" in "\n".join(win.call("chat_screen").get("screens", [])), 90, 3) \
        if f"B got: {lines[-1]}" not in screen else True
    return echoed, "B got all 10; " + ("A shows B's echoes" if echoed else "A's screen lacks the last echo")


def s_disconnect_ab(a, win, b):
    win.call("chat_disconnect")
    ok = wait(lambda: (c := b.connection_with(a.a_call)) is None or not c.connected, 60)
    return ok, "B saw the disconnect" if ok else "B still connected"


def s_connect_ba(a, win, b):
    b.conn = b.eng.connect(a.a_call)
    if not wait(lambda: b.conn.connected, 90):
        return False, f"B's connect ended {b.conn.state}"
    ok = wait(lambda: any(f"Connected to {a.b_call}" in t for t in win.call("chat_state").get("labels", [])), 30, 2)
    return ok, "connected both ends" if ok else "B connected, A's window does not say so"


def s_chat_ba(a, win, b):
    lines = [f"B line {i + 1:02d} {_stamp()}" for i in range(10)]
    for line in lines:
        b.conn.send(line + "\r")
    if not wait(lambda: b.conn.pending == 0, 180, 1):
        return False, f"{b.conn.pending} frame(s) never acknowledged by A"
    ok = wait(lambda: lines[-1] in "\n".join(win.call("chat_screen").get("screens", [])), 30, 3)
    return ok, "all acked by A; " + ("on A's screen" if ok else "last line NOT on A's screen")


def s_transfer_ba(a, win, b):
    body = "".join(f"{i:04d} the quick brown fox jumps over the lazy dog 0123456789\r" for i in range(32))
    payload = (body[:2048 - 12] + "END-OF-2KB\r").encode()
    t = time.time()
    b.conn.send(payload)
    if not wait(lambda: b.conn.pending == 0, 240, 1):
        return False, f"{b.conn.pending} frame(s) never acknowledged after {time.time() - t:.0f} s"
    took = time.time() - t
    ok = wait(lambda: "END-OF-2KB" in "\n".join(win.call("chat_screen").get("screens", [])), 30, 3)
    return ok, f"{len(payload)} bytes acked in {took:.0f} s ({len(payload) / took:.0f} B/s); " + \
        ("end marker on A's screen" if ok else "end marker NOT on A's screen")


def s_disconnect_ba(a, win, b):
    b.conn.close()
    if not wait(lambda: not b.conn.connected, 60):
        return False, "B's disconnect did not complete"
    ok = wait(lambda: any(f"Disconnected from {a.b_call}" in t for t in win.call("chat_state").get("labels", [])), 30, 2)
    return ok, "both ends disconnected" if ok else "A's window does not show the disconnect"


SCENARIO_FUNCS = {"aprs-ab": s_aprs_ab, "aprs-ba": s_aprs_ba, "connect-ab": s_connect_ab, "chat-ab": s_chat_ab,
                  "disconnect-ab": s_disconnect_ab, "connect-ba": s_connect_ba, "chat-ba": s_chat_ba,
                  "transfer-ba": s_transfer_ba, "disconnect-ba": s_disconnect_ba}


# -- stuck transmitters ------------------------------------------------------------------------

def cmd_stuck(a, conf) -> int:
    need_keyed(a)
    only = a.only.split(",") if a.only else ["a-killed", "b-killed"]
    results = {}
    if "a-killed" in only:
        results["a-killed"] = stuck_a(a, conf)
    if "b-killed" in only:
        results["b-killed"] = stuck_b(a)
    if "b-frozen" in only:
        results["b-frozen"] = stuck_b(a, "frozen")
    LOG.info("RESULTS\n  %s", "\n  ".join(f"{k}: {v}" for k, v in results.items()))
    return 0 if all(v.startswith(("PASS", "SKIP")) for v in results.values()) else 1


def stuck_a(a, conf) -> str:
    """A connects to B and sends ~2 KB of chat, so its overs run several
    seconds; 1.5 s into one, the app is killed. B times the carrier."""
    airtime = Airtime(a.max_airtime_s, a.max_key_a_s)
    win = WinA()
    win.configure(call=a.a_call.split("-")[0], ssid=int(a.a_call.split("-")[1]), freq=a.freq, transmit=True,
                  conf=conf)
    win.start_agent()
    b = StationB(a, airtime, keyed=True).open()
    b.echo = False                                  # B stays quiet but for its acks
    b.on_a_overlong = lambda s: emergency_a(win, b, conf, s)
    try:
        win.call("start_app", timeout=60)
        if not wait(lambda: any(t.endswith(": Connected") for t in win.call("status").get("bar", [])), 40, 2):
            return "FAIL A's radio did not connect"
        if not win.call("arm", keyed=True).get("armed"):
            return "FAIL A would not arm"
        win.call("connect", call=a.b_call)
        if not wait(lambda: (c := b.connection_with(a.a_call)) is not None and c.connected, 60):
            return "FAIL no connection to run the test on"
        n_edges = len(b.watch.edges)
        lines = [f"stuck test {i:02d} " + "x" * 90 for i in range(20)]
        threading.Thread(target=win.call, args=("chat_send",), kwargs={"lines": lines, "gap_s": 0.05,
                                                                       "timeout": 120}, daemon=True).start()
        # Wait for a carrier from A that has lasted 1.5 s, then kill the app.
        if not wait(lambda: b.watch.on and time.monotonic() - b.watch.since > 1.5, 90, 0.05):
            return "FAIL B never heard a long enough over from A"
        killed_at = time.monotonic()
        win.kill_app_now()
        LOG.info("A: killed mid-over (carrier up %.1f s)", killed_at - b.watch.since)
        gone = wait(lambda: not b.watch.on, 10, 0.05)
        after = (b.watch.last_off - killed_at) if gone and b.watch.last_off else None
        LOG.info("carrier %s", f"stopped {after:.2f} s after the kill" if after is not None else "STILL UP after 10 s")
        if not gone:
            emergency_a(win, b, conf, time.monotonic() - b.watch.since)
        r = icom_direct(conf, unkey=False, deadline_s=90)
        LOG.info("IC-9700 read over CI-V from shack after the kill: %s", r)
        ok = gone and r.get("ptt") == "1c 00 00"
        verdict = (f"carrier stopped {after:.2f} s after the kill" if gone else "carrier outlived the kill") + \
            f"; PTT read {r.get('ptt')} ({r.get('tries')} login tries); RF power {r.get('rf_power')}"
        return ("PASS " if ok else "FAIL ") + verdict
    finally:
        b.close()
        LOG.info(airtime.summary())


def stuck_b(a, how: str = "killed") -> str:
    """A child process keys the Pluto for a long over and 1.5 s in is
    SIGKILLed (`killed`) or SIGSTOPped (`frozen`, then killed once judged);
    the guard it started must quiet the transmitter: at once for a kill (the
    on-device guard sees its session close), within its 3 s heartbeat
    timeout for a freeze. With --device-guard the guard is the one on the
    Pluto; without, pluto_guard.py on shack (which cannot see a freeze, so
    `frozen` is only run with --device-guard)."""
    if how == "frozen" and not a.device_guard:
        return "SKIP b-frozen needs --device-guard"
    child = subprocess.Popen([sys.executable, str(HERE / "twostation.py"), "_b_long_over", "--keyed",
                              "--operator-present", "--freq", str(a.freq), "--pluto-tx-gain-db",
                              str(a.pluto_tx_gain_db), "--max-key-s", str(a.max_key_s)]
                             + (["--device-guard"] if a.device_guard else []))
    try:
        if not wait(lambda: read_pluto_tx()[0] == "0", 30, 0.1):
            return "FAIL the child never keyed"
        time.sleep(1.5)
        os.kill(child.pid, signal.SIGKILL if how == "killed" else signal.SIGSTOP)
        killed = time.monotonic()
        if how == "killed":
            child.wait(5)
        quiet = wait(lambda: read_pluto_tx()[0] == "1", 5, 0.05)
        took = time.monotonic() - killed
        state = read_pluto_tx()
        return ("PASS " if quiet else "FAIL ") + f"Pluto {'quiet' if quiet else 'NOT QUIET'} {took:.2f} s after " \
            f"{'SIGKILL' if how == 'killed' else 'SIGSTOP'} ({state})"
    finally:
        if child.poll() is None:
            child.kill()
        pd, gain = read_pluto_tx()
        if pd != "1":
            from pluto_guard import Guard
            LOG.error("Pluto still up after the b-killed test; quieting: %s", Guard(PLUTO_URI, load_iio()).quiet())


def cmd_b_long_over(a, conf) -> int:
    """The child of stuck_b: key the Pluto with ~6 s of frames under its own
    guard, then wait to be killed."""
    need_keyed(a)
    from shack_packet import afsk
    from shack_packet.aprs import TOCALL
    from shack_packet.ax25 import Frame
    from shack_packet.identity import Station
    from shack_packet.radios import PlutoLink
    st = Station.of(a.b_call, tx_enabled=True)
    link = PlutoLink(PLUTO_URI, frequency=a.freq, tx_gain_db=a.pluto_tx_gain_db, iio_module=load_iio(),
                     station=st, max_key_s=a.max_key_s, **b_guard(a))

    class Sink:
        station = st

        def pcm_in(self, pcm): pass
        def frame_in(self, f): pass
        def busy_in(self, b): pass
        def link_event(self, *e): pass

    link.attach(Sink())
    link.open()
    host_guard = None
    if not a.device_guard:
        host_guard = subprocess.Popen([sys.executable, str(HERE / "pluto_guard.py"), "--uri", PLUTO_URI,
                                       "--pid", str(os.getpid()), "--max-key-s", str(a.max_key_s)])
    elif not link.tx_guarded:
        LOG.error("B: the guard on the Pluto did not start (%s); not keying", link.unguarded_why)
        return 1
    frames = [Frame.from_monitor(f"{a.b_call}>{TOCALL}:>stuck-TX test {i}").encode() for i in range(12)]
    pcm = afsk.pcm16(afsk.modulate(frames, 12_000, amplitude=0.5, txdelay_ms=300))
    link.key()
    link.send_pcm(pcm)
    time.sleep(a.max_key_s + 5)          # killed long before this; if not, the link's own limits act
    link.close()
    if host_guard is not None:
        host_guard.terminate()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["check", "rx-verify", "listen", "run", "stuck", "install-a",
                                                "_b_long_over"])
    ap.add_argument("installer", nargs="?", help="install-a: the Windows installer .exe")
    ap.add_argument("--seconds", type=float, default=60.0, help="listen: how long")
    ap.add_argument("--keyed", action="store_true")
    ap.add_argument("--operator-present", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--freq", type=int, default=432_650_000)
    ap.add_argument("--a-call", default="GM4BRI-1")
    ap.add_argument("--b-call", default="GM4BRI-2")
    ap.add_argument("--pluto-tx-gain-db", type=float, default=-10.0)
    ap.add_argument("--pluto-rx-gain-db", type=float, default=25.0)
    ap.add_argument("--max-key-s", type=float, default=8.0)
    ap.add_argument("--device-guard", action="store_true",
                    help="station B uses the guard on the Pluto (shack_pluto) instead of pluto_guard.py on shack")
    ap.add_argument("--max-key-a-s", type=float, default=17.0)
    ap.add_argument("--max-airtime-s", type=float, default=240.0)
    ap.add_argument("--radio-json", default=str(Path.home() / ".config" / "ohs-twostation" / "ic9700.json"))
    a = ap.parse_args(argv)
    logdir = Path.home() / "build" / "ohs-twostation"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"{a.command.strip('_')}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(logfile)])
    for noisy in ("shack_rig", "shack_packet"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.path.insert(0, str(HERE))
    conf = json.loads(Path(a.radio_json).read_text()) if Path(a.radio_json).exists() else {}
    LOG.info("twostation %s: %s, log %s", a.command, vars(a) | {"radio_json": "(given)"}, logfile)
    try:
        return {"check": cmd_check, "rx-verify": cmd_rx_verify, "run": cmd_run, "stuck": cmd_stuck,
                "install-a": cmd_install_a, "listen": cmd_listen, "_b_long_over": cmd_b_long_over}[a.command](a, conf)
    finally:
        for w in _OPEN:
            w.close()


if __name__ == "__main__":
    sys.exit(main())
