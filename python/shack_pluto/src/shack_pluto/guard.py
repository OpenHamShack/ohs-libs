"""The transmitter guard that runs on the Pluto itself.

**Why it exists.** A program keying a Pluto quiets the transmitter after
every over, at close, on any error and from watchdog threads of its own
(shack_packet's PlutoLink; QO-100 NB's ohscore/devices/pluto.py) - and every
one of those runs inside the process that keyed it. Kill that process
(`kill -9`, a native crash, the laptop's battery) and none of them runs:
the AD9361 keeps whatever its attributes last said, so the TX LO stays
powered at the over's gain and radiates until something quiets it, for
hours if nobody is at the desk. That is the state a rebooted Pluto comes up
in (GUIDE §5, "a Pluto reboot keys its TX LO"). A packaged desktop app
cannot leave a second host process behind to watch (the two-station rig's
tools/pluto_guard.py does, and only works because a person starts it), and
stock iiod has no hook to quiet on disconnect. So the watcher runs on the
Pluto, where the process dying cannot reach it.

**What it is.** A short busybox `sh` script (GUARD_SCRIPT), sent as the
remote command of an SSH session the app opens, so nothing is written to
the Pluto's flash and nothing is left installed: it lives exactly as long
as that session. It:

- finds ad9361-phy by name under /sys/bus/iio/devices, writes the two quiet
  values (TX LO powerdown 1, TX hardwaregain -89.75 dB) through sysfs,
  reads them back, and only then prints `ready`;
- reads its stdin, where the app writes a line a second (`Heartbeat`), and
  quiets, says why and exits:
  - at **EOF** - the SSH session closed, which is what a killed or crashed
    app looks like from the Pluto (the TCP connection closes, dropbear
    closes the command's stdin);
  - when **no line has come for HEARTBEAT_TIMEOUT_S** (3 s) - an app that is
    frozen (SIGSTOP, a hung interpreter), or a host that lost power or its
    network, where no FIN ever arrives;
  - on SIGHUP, SIGTERM, SIGINT or SIGPIPE, and on a `q` line (a clean close);
- and while it runs, quiets (and carries on watching) when TX LO powerdown
  has read 0 for longer than max_key_s + OVERLONG_MARGIN_S - the same limit
  as the app's own watchdogs plus two seconds, so it only acts when they
  have not.

It writes only those two quiet values, never anything that could key. Its
log is a few lines in /tmp on the Pluto (RAM, not flash), for an R3-style
proof afterwards.

**Measured on the two firmwares we have (18/10/2026, receive-only, the
transmitter quiet throughout):** the Rev.B clone on ADI-derived v0.39 has
BusyBox 1.36.1, the Tezuka nano (tezuka-v0.3.21) BusyBox 1.37.0; both run
dropbear, both answer `root`/`analog`, and both have the sysfs names used
here. `read -t` takes fractions of a second on both (0.3 s timed out in
0.31 s). It does NOT return the same status on a timeout: 1 on 1.36.1, 142
(128 + SIGALRM) on 1.37.0 - and EOF is 1 on both. So the script tells EOF
from a timeout by the clock, not the status: a `read` that failed in less
than EOF_FAST_CS hundredths of a second did not wait, so its stdin is
closed. PlutoDVB2 has not been checked [guess: busybox and dropbear too].

**Proved on both, the same day, with nothing keyed:** the guard came up
`ready` 0.6 s after the SSH login began; a client killed with `kill -9`
(paramiko on shack, OpenSSH on workbench) had its guard log `quiet eof`
within half a second; a client frozen with SIGSTOP, or one whose pipe
stayed open with nothing in it, had `quiet heartbeat` 3.0-3.6 s after its
last line; a clean close logged `quiet closed`; and no guard process was
left behind. What none of that could show is a carrier going off, because
nothing was keyed: that is session R3, on a dummy load with Brian present.

**Fail closed.** `Guard.confirmed` is true only between the `ready` line and
the guard going away (its stdout ending, or a line saying it quieted for
any reason but an over-long key). A link that is given a guard refuses to
key without a confirmed one (PlutoLink, "tx-unguarded"), so a Pluto whose
guard cannot be started - no SSH, a wrong password, a USB-only URI, a
firmware the script does not suit - opens receive-only and says why.

The transport is a `Runner`: `SshRunner` (paramiko, which the desktop
bundles carry with cryptography) for a real Pluto, `LocalRunner` for the
tests, which run this very script under the host's busybox or sh against
a stand-in sysfs.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
import time
from typing import Callable, Protocol

log = logging.getLogger(__name__)

HEARTBEAT_S = 1.0
HEARTBEAT_TIMEOUT_S = 3.0
OVERLONG_MARGIN_S = 2.0
#: How long start() waits for `ready`. An SSH login to a Pluto over USB
#: networking takes about a second (dropbear's key exchange on the Zynq is
#: most of it), but PlutoDVB2 0.5.16.7's dropbear takes 5.9 s to send its
#: banner (measured through the controller box, 18/10/2026), and a Pluto
#: shared over a network adds its own: 25 s, with SSH_TIMEOUT_S under it.
READY_TIMEOUT_S = 25.0
#: SshRunner's connect, banner and authentication timeouts.
SSH_TIMEOUT_S = 20.0
QUIET_GAIN = "-89.75"

#: The guard, as busybox ash runs it. Arguments: $1 max_key_s (whole
#: seconds), $2 the heartbeat timeout (seconds), $3 a root to find sysfs
#: under ("" on a Pluto; the tests' stand-in tree), $4 "gpo" when the link
#: drives a GPO as PTT (radios/pluto.py `ptt_output`): every quiet then
#: also sets the AD936x's GPO outputs low (register 0x27 = 0, through
#: debugfs, as PlutoDVB2's own watchconsoletx.sh does on mute); $5 the
#: most TX gain allowed (whole dB, e.g. -20, an amplifier's drive cap;
#: "" for none): a gain read above it quiets at once ("quiet overdrive"),
#: so a process that wrote one by mistake, or died having done so, cannot
#: overdrive the amplifier. No single quotes inside: it is passed as one
#: quoted word.
GUARD_SCRIPT = r"""
LIMIT=$(( ($1 + 2) * 100 )); HB=$(( $2 * 100 )); ROOT=$3; GPO=$4; CAP=$5; EOF_FAST_CS=5
LOG=/tmp/shack-pluto-guard.log; [ -n "$ROOT" ] && LOG=$ROOT/guard.log
D=
for d in $ROOT/sys/bus/iio/devices/iio:device*; do
  [ "$(cat $d/name 2>/dev/null)" = ad9361-phy ] && D=$d
done
if [ -z "$D" ]; then echo "error no-ad9361-phy"; exit 2; fi
[ -n "$GPO" ] && [ ! -e $ROOT/sys/kernel/debug/iio/${D##*/}/direct_reg_access ] && { echo "error no-gpo-access"; exit 4; }
PD=$D/out_altvoltage1_TX_LO_powerdown
G=$D/out_voltage0_hardwaregain
REG=$ROOT/sys/kernel/debug/iio/${D##*/}/direct_reg_access
now() { u=$(cut -d" " -f1 /proc/uptime); echo $(( ${u%.*} * 100 + 1${u#*.} - 100 )); }
say() { echo "$(now) $*" >> $LOG; echo "$*"; }
quiet() { echo 1 > $PD; echo -89.75 > $G; [ -n "$GPO" ] && echo "0x27 0x0" > $REG 2>/dev/null; }
isquiet() { [ "$(cat $PD)" = 1 ] || return 1; case "$(cat $G)" in -89.75*) return 0;; esac; return 1; }
leave() { [ -n "$LEFT" ] && { quiet; exit 0; }; LEFT=1; quiet; if isquiet; then say "quiet $1"; else say "not-quiet $1"; fi; exit 0; }
trap "leave hangup" HUP
trap "leave terminated" TERM INT
trap "leave pipe" PIPE
: > $LOG
quiet
if ! isquiet; then say "error not-quiet"; exit 3; fi
say "ready pid=$$ limit=$LIMIT hb=$HB"
last=$(now); on=
while :; do
  t0=$(now)
  if read -t 0.5 line; then
    last=$(now)
    [ "$line" = q ] && leave closed
  else
    [ $(( $(now) - t0 )) -lt $EOF_FAST_CS ] && leave eof
  fi
  n=$(now)
  [ $(( n - last )) -gt $HB ] && leave heartbeat
  if [ -n "$CAP" ]; then
    g=$(cat $G); g=${g%% *}; g=${g%%.*}
    if [ "$g" -gt "$CAP" ] 2>/dev/null; then
      quiet
      if isquiet; then say "quiet overdrive"; else say "not-quiet overdrive"; fi
    fi
  fi
  if [ "$(cat $PD)" = 0 ]; then
    [ -z "$on" ] && on=$n
    if [ $(( n - on )) -gt $LIMIT ]; then
      quiet; on=
      if isquiet; then say "quiet overlong"; else say "not-quiet overlong"; fi
    fi
  else
    on=
  fi
done
"""


def guard_command(max_key_s: float, heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S, root: str = "",
                  gpo: bool = False, max_gain_db: float | None = None) -> str:
    """The remote command: `sh -c <script> name args`, one line, quoted.
    `max_gain_db` is rounded down to a whole dB for the shell's arithmetic:
    the guard acts on a gain a whole dB over it, the link (radios/pluto.py)
    refuses anything over it exactly."""
    max_key = max(1, int(round(max_key_s)))
    hb = max(1, int(round(heartbeat_timeout_s)))
    args = ["sh", "-c", GUARD_SCRIPT, "shack-pluto-guard", str(max_key), str(hb), root]
    if gpo or max_gain_db is not None:
        args.append("gpo" if gpo else "")
    if max_gain_db is not None:
        import math
        args.append(str(math.floor(max_gain_db)))
    return " ".join(shlex.quote(a) for a in args)


class Session(Protocol):
    """One running remote command: a line in, lines out."""

    def write(self, data: bytes) -> None: ...
    def readline(self) -> bytes: ...       # b"" at the end
    def close(self) -> None: ...


class Runner(Protocol):
    def start(self, command: str) -> Session: ...


class GuardError(Exception):
    """Why a guard is not running: `reason` is a key the app translates
    (REASONS below), `detail` the words that came with it."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


#: Every reason a guard gives, for the app's translations.
REASONS = {
    "guard-no-ssh": "the transmitter guard could not log in to the Pluto over SSH: check its address and password",
    "guard-no-network": "the transmitter guard needs the Pluto's network address (ip:...), not a USB address",
    "guard-failed": "the transmitter guard did not start on the Pluto",
    "guard-not-quiet": "the transmitter guard could not quiet the Pluto's transmitter",
    "guard-no-gpo": "the transmitter guard cannot reach the Pluto's GPO outputs (no debugfs), so the PTT output "
                    "cannot be guarded",
    "guard-lost": "the transmitter guard on the Pluto stopped, so transmitting is off",
}


class Guard:
    """The client end (module docstring). `start()` raises GuardError, or
    returns once the guard has said `ready`; then `confirmed` stays true
    until the guard goes. `on_line(line)` hears every line it prints,
    from its reader thread."""

    def __init__(self, runner: Runner, max_key_s: float, *, heartbeat_s: float = HEARTBEAT_S,
                 heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S, root: str = "",
                 on_line: Callable[[str], None] | None = None, gpo: bool = False,
                 max_gain_db: float | None = None):
        self.runner = runner
        #: The amplifier's drive cap the guard keeps (GUARD_SCRIPT's $5).
        self.max_gain_db = max_gain_db
        #: Also drop the GPO outputs at every quiet (GUARD_SCRIPT's $4).
        self.gpo = gpo
        self.max_key_s = max_key_s
        self.heartbeat_s = heartbeat_s
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.root = root
        self.on_line = on_line
        self.lines: list[str] = []
        self.why_lost: str | None = None
        self._session: Session | None = None
        self._ready = threading.Event()
        self._gone = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def confirmed(self) -> bool:
        return self._ready.is_set() and not self._gone.is_set()

    def start(self, timeout: float = READY_TIMEOUT_S) -> Guard:
        if self.confirmed:
            return self
        self._ready.clear()
        self._gone.clear()
        self._stop.clear()
        self.why_lost = None
        try:
            self._session = self.runner.start(guard_command(self.max_key_s, self.heartbeat_timeout_s, self.root,
                                                            self.gpo, self.max_gain_db))
        except GuardError:
            raise
        except Exception as e:                      # noqa: BLE001 - any failure to connect is "no ssh"
            raise GuardError("guard-no-ssh", str(e)) from None
        reader = threading.Thread(target=self._read, name="Pluto guard reader", daemon=True)
        reader.start()
        self._threads = [reader]
        if not self._ready.wait(timeout) or self._gone.is_set():
            last = self.lines[-1] if self.lines else "no answer"
            self.stop()
            reason = ("guard-not-quiet" if "not-quiet" in last else "guard-no-gpo" if "no-gpo" in last
                      else "guard-failed")
            raise GuardError(reason, last)
        beat = threading.Thread(target=self._beat, name="Pluto guard heartbeat", daemon=True)
        beat.start()
        self._threads.append(beat)
        return self

    def stop(self) -> None:
        """A clean close: the guard quiets once more and exits."""
        self._stop.set()
        session, self._session = self._session, None
        if session is not None:
            try:
                session.write(b"q\n")
            except Exception:                       # noqa: BLE001 - closing regardless
                pass
            try:
                session.close()
            except Exception:                       # noqa: BLE001
                pass
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2)
        self._threads = []
        self._gone.set()

    def _read(self) -> None:
        session = self._session
        try:
            while session is not None:
                raw = session.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self.lines.append(line)
                if line.startswith("ready"):
                    self._ready.set()
                elif line.startswith(("quiet ", "not-quiet ", "error ")):
                    if line == "quiet overlong":
                        log.error("Pluto guard: the transmitter was up past its limit; the guard quieted it")
                    elif line == "quiet overdrive":
                        log.error("Pluto guard: the TX gain was above the amplifier's cap; the guard quieted it")
                    else:
                        self.why_lost = line
                        log.warning("Pluto guard: %s", line)
                if self.on_line is not None:
                    try:
                        self.on_line(line)
                    except Exception:               # noqa: BLE001 - the listener's problem
                        log.exception("Pluto guard listener")
        except Exception as e:                      # noqa: BLE001 - a broken session is a lost guard
            self.why_lost = self.why_lost or f"session failed: {e}"
        finally:
            if not self._stop.is_set() and self._ready.is_set():
                log.error("Pluto guard: gone (%s); transmitting is off until it is started again",
                          self.why_lost or "its session ended")
            self._gone.set()

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            session = self._session
            if session is None or self._gone.is_set():
                return
            try:
                session.write(b"h\n")
            except Exception as e:                  # noqa: BLE001 - the guard will notice too
                self.why_lost = self.why_lost or f"heartbeat failed: {e}"
                self._gone.set()
                return


# -- runners --------------------------------------------------------------------------


class _PipeSession:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def write(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def readline(self) -> bytes:
        return self.proc.stdout.readline()

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def kill(self) -> None:
        """What a crashed app looks like: the pipe closes with no `q`."""
        self.proc.stdin.close()


class LocalRunner:
    """Runs the command in a local shell, for the tests: busybox's `sh`
    when the host has one, because that is the Pluto's shell, and a
    script that only works in bash or dash proves nothing about it (the
    host's /bin/sh is dash, whose `read` has no -t, and a guard under it
    quits at once - closed, but not what we are testing)."""

    def __init__(self):
        self.sessions: list[_PipeSession] = []
        self.busybox = shutil.which("busybox")

    def start(self, command: str) -> _PipeSession:
        if self.busybox and command.startswith("sh "):
            command = f"{shlex.quote(self.busybox)} {command}"
        proc = subprocess.Popen([self.busybox or "/bin/sh", "sh", "-c", command] if self.busybox
                                else ["/bin/sh", "-c", command],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        s = _PipeSession(proc)
        self.sessions.append(s)
        return s


class _SshSession:
    def __init__(self, client, channel):
        self.client, self.channel = client, channel
        self._out = channel.makefile("rb")

    def write(self, data: bytes) -> None:
        self.channel.sendall(data)

    def readline(self) -> bytes:
        return self._out.readline()

    def close(self) -> None:
        try:
            self.channel.shutdown_write()
            # Long enough for `quiet closed` to come back, so the log has it.
            end = time.monotonic() + 1.0
            while not self.channel.exit_status_ready() and time.monotonic() < end:
                time.sleep(0.02)
        finally:
            self.client.close()


class SshRunner:
    """paramiko to the Pluto: `root` and the firmware's password (ADI's
    default `analog`; Tezuka's is the same, measured 18/10/2026). The host
    key is not checked: a Pluto makes a new one at every boot (its root
    filesystem is RAM), so pinning one would refuse every Pluto after its
    first reboot, and the guard sends nothing secret."""

    def __init__(self, host: str, password: str = "analog", user: str = "root", port: int = 22,
                 timeout: float = SSH_TIMEOUT_S):
        self.host, self.password, self.user, self.port, self.timeout = host, password, user, port, timeout

    @classmethod
    def for_uri(cls, uri: str, password: str = "analog", ssh_port: int | None = None) -> SshRunner:
        """A Pluto's URI (endpoint.py: `ip:host`, `ip:host:iiodport`,
        `pluto://host?iiod=..&ssh=..`, a bare host) -> a runner on its SSH
        port; a USB URI has no network address to reach, which is
        GuardError guard-no-network."""
        from .endpoint import EndpointError, parse
        try:
            ep = parse(uri, ssh_port)
        except EndpointError as e:
            raise GuardError("guard-no-network", str(e)) from None
        return cls(ep.host, password, port=ep.ssh_port)

    def connect(self):
        try:
            import paramiko
        except ImportError as e:
            raise GuardError("guard-no-ssh", f"no SSH client in this build ({e})") from None
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self.host, port=self.port, username=self.user, password=self.password,
                           timeout=self.timeout, banner_timeout=self.timeout, auth_timeout=self.timeout,
                           allow_agent=False, look_for_keys=False)
        except Exception as e:                      # noqa: BLE001 - auth, refused, unreachable: all "no ssh"
            client.close()
            raise GuardError("guard-no-ssh", f"{self.user}@{self.host}: {e}") from None
        transport = client.get_transport()
        if transport is not None:
            # A keepalive, so a session whose host vanished is noticed on
            # this side too; the guard's own heartbeat timeout is what
            # protects the radio.
            transport.set_keepalive(2)
        return client

    def start(self, command: str) -> _SshSession:
        client = self.connect()
        channel = client.get_transport().open_session()
        channel.exec_command(command)
        return _SshSession(client, channel)

    def run(self, command: str, timeout: float = 10.0) -> tuple[int, str]:
        """One command to completion: (exit status, stdout+stderr text)."""
        client = self.connect()
        try:
            channel = client.get_transport().open_session()
            channel.set_combine_stderr(True)
            channel.settimeout(timeout)
            channel.exec_command(command)
            out = channel.makefile("rb").read().decode("utf-8", "replace")
            return channel.recv_exit_status(), out
        finally:
            client.close()

    def detach(self, command: str) -> None:
        """Start a command that must outlive this SSH session (a reboot):
        `setsid` gives it a session of its own, so dropbear's hang-up as
        the connection closes does not reach it, and its output goes
        nowhere, so it never blocks on a closed pipe. A plain `cmd &` dies
        with the session (D5, Brian 18/10/2026). Measured on shack's clone
        18/10/2026: this returns in 0.3 s and the command ran after the
        session closed; a plain `cmd &` through run() only appeared to
        survive because run() waits for the output to end, which a
        background job holding stdout keeps open - with a reboot, that is
        the session dying under it."""
        wrapped = f"setsid sh -c {shlex.quote(command)} </dev/null >/dev/null 2>&1 &"
        self.run(wrapped, timeout=5.0)
