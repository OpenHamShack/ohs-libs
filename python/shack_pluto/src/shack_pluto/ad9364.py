"""The range check and the one-click AD9364 enable (PLAN-0.2 §3.6.2, D5).

A stock Pluto runs its AD9363 as itself: 325 MHz-3.8 GHz, no 2 m and no
70 cm below 325. ADI's "customizing" page sets the chip to AD9364 in the
U-Boot environment, after which it tunes 70 MHz-6 GHz. The steps, as Brian
settled them on 18/10/2026:

1. **The operator confirms a dummy load, or no antenna, on TX** - because
   step 4 reboots the Pluto, and a rebooting Pluto comes up with its TX LO
   powered at 2450 MHz and -10 dB (GUIDE §5, "a Pluto reboot keys its TX
   LO"): a reboot is a transmission.
2. **Back up the environment**: `fw_printenv`, saved on this computer under
   the Pluto's key (identity.PlutoIdentity.key) with the time.
3. **Three `fw_setenv`s**: `attr_name compatible`, `attr_val ad9364`, and
   `compatible ad9364` - ADI firmware v0.32 and later needs the third, and
   the first two alone leave an AD9363 (HANDOVER, 18/10 ~11:24). Each is read
   back with `fw_printenv` before the reboot.
4. **Reboot under a TX-LO quiet watcher.** The watcher starts first and
   waits for the Pluto to go away - so it never acts on the Pluto as it was
   before the reboot - then polls iiod until it answers and writes quiet
   (powerdown 1, -89.75 dB) and reads it back, the moment it can, and keeps
   re-quieting for QUIET_HOLD_S in case something at boot powers it again.
   The reboot itself runs detached on the Pluto (`setsid`): a backgrounded
   `reboot &` dies with the SSH session that started it (D5).
5. **Verify** the new chip setting and range from a fresh context.

Refused, with what to do instead, before anything is changed:

- on any firmware but ADI's and its clones (`identity.CAN_ENABLE_AD9364`):
  Tezuka and PlutoDVB2 are refused until the method is verified on each;
- without the operator's confirmation;
- when it is already done (the chip already reads ad9364 or ad9361).

NOT YET RUN ON A PLUTO: the reboot radiates, so it waits for session R3
(dummy load, Brian present). Shack's clone already reads ad9364, so the
steps up to the reboot would be a no-op there; the tests drive a stand-in.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .identity import PlutoIdentity, identify

log = logging.getLogger(__name__)

SETENV = (("attr_name", "compatible"), ("attr_val", "ad9364"), ("compatible", "ad9364"))
#: How long the watcher waits for the Pluto to go away after the reboot is
#: sent, and then to come back.
GONE_TIMEOUT_S = 30.0
BACK_TIMEOUT_S = 120.0
POLL_S = 0.1
#: After the first quiet, keep quieting this long: a boot script that
#: brings the transmitter up after iiod starts is caught too.
QUIET_HOLD_S = 10.0
QUIET_GAIN = "-89.75"

#: Every reason it gives, for the app's translations.
REASONS = {
    "enable-firmware": "this Pluto's firmware is not one the AD9364 setting has been checked on; "
                       "use ADI's 'customizing' page by hand, or ADI firmware",
    "enable-unconfirmed": "confirm first that a dummy load, or nothing, is on the Pluto's TX port: "
                          "it transmits briefly as it reboots",
    "enable-done": "this Pluto is already set up for 70 MHz-6 GHz",
    "enable-ssh": "the Pluto's settings could not be changed over SSH",
    "enable-verify": "the Pluto's settings did not read back as written; nothing was rebooted",
    "enable-not-back": "the Pluto did not come back after its reboot: check it, and quiet it if it is transmitting",
    "enable-not-quiet": "the Pluto came back but its transmitter would not read back quiet: switch it off",
    "enable-range": "the Pluto came back, but not set up for 70 MHz-6 GHz",
}


class EnableError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


def parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@dataclass
class EnableResult:
    backup: Path | None = None
    gone_after_s: float | None = None
    back_after_s: float | None = None
    quiet_after_s: float | None = None     # from iiod first answering to quiet read back
    requiets: int = 0
    after: PlutoIdentity | None = None
    steps: list = field(default_factory=list)


def needs_enable(ident: PlutoIdentity, hz: float) -> bool:
    """Whether `hz` is out of reach as the chip is set up now, and an AD9364
    setting would bring it in (70 MHz-6 GHz)."""
    covered = ident.covers(hz)
    if covered is None:
        return ident.chip == "ad9363" and 70e6 <= hz <= 6e9
    return not covered and 70e6 <= hz <= 6e9


class QuietWatcher:
    """Step 4's watcher, in a thread of its own (module docstring). It
    writes only the two quiet values, through a fresh context each time."""

    def __init__(self, iio, uri: str, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, gone_timeout_s: float = GONE_TIMEOUT_S,
                 back_timeout_s: float = BACK_TIMEOUT_S, hold_s: float = QUIET_HOLD_S):
        self.iio, self.uri, self.clock, self.sleep = iio, uri, clock, sleep
        self.gone_timeout_s, self.back_timeout_s, self.hold_s = gone_timeout_s, back_timeout_s, hold_s
        self.result = EnableResult()
        self.error: EnableError | None = None
        self.ctx = None
        self._armed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="Pluto reboot quiet watcher", daemon=True)

    def start(self) -> QuietWatcher:
        self._thread.start()
        return self

    def armed(self) -> None:
        """The reboot has been sent: start timing the Pluto's going away."""
        self._armed.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _context(self):
        try:
            return self.iio.Context(self.uri)
        except Exception:                           # noqa: BLE001 - not there (yet)
            return None

    def _chans(self, ctx):
        phy = ctx.find_device("ad9361-phy")
        if phy is None:
            return None
        return phy.find_channel("altvoltage1", True), phy.find_channel("voltage0", True)

    def _quiet(self, ctx) -> bool:
        chans = self._chans(ctx)
        if chans is None or None in chans:
            return False
        lo, tx = chans
        for attr, value in ((lo.attrs["powerdown"], "1"), (tx.attrs["hardwaregain"], QUIET_GAIN)):
            try:
                attr.value = value
            except (OSError, KeyError) as e:
                log.error("reboot watcher: quieting failed: %s", e)
        try:
            pd = str(lo.attrs["powerdown"].value).strip()
            gain = float(str(tx.attrs["hardwaregain"].value).split()[0])
        except (OSError, KeyError, ValueError):
            return False
        return pd == "1" and gain <= float(QUIET_GAIN) + 0.01

    def _run(self) -> None:
        try:
            self._armed.wait()
            t0 = self.clock()
            # Gone: a context that cannot be made.
            while True:
                ctx = self._context()
                if ctx is None:
                    self.result.gone_after_s = self.clock() - t0
                    break
                del ctx
                if self.clock() - t0 > self.gone_timeout_s:
                    raise EnableError("enable-not-back", "the Pluto never went away after the reboot was sent")
                self.sleep(POLL_S)
            # Back: the first context, quieted at once.
            while True:
                ctx = self._context()
                if ctx is not None:
                    back = self.clock()
                    self.result.back_after_s = back - t0
                    break
                if self.clock() - t0 > self.gone_timeout_s + self.back_timeout_s:
                    raise EnableError("enable-not-back")
                self.sleep(POLL_S)
            while not self._quiet(ctx):
                if self.clock() - back > 5.0:
                    raise EnableError("enable-not-quiet")
                self.sleep(POLL_S)
                ctx = self._context() or ctx
            self.result.quiet_after_s = self.clock() - back
            log.warning("Pluto %s back %.1f s after its reboot; transmitter quiet %.2f s after iiod answered",
                        self.uri, self.result.back_after_s, self.result.quiet_after_s)
            # Hold: keep it quiet while the rest of the boot runs.
            end = self.clock() + self.hold_s
            while self.clock() < end:
                self.sleep(POLL_S * 5)
                chans = self._chans(ctx)
                try:
                    up = chans is not None and str(chans[0].attrs["powerdown"].value).strip() != "1"
                except (OSError, KeyError):
                    up = True
                if up:
                    self.result.requiets += 1
                    log.error("Pluto %s: the transmitter came up again during boot; quieting", self.uri)
                    self._quiet(ctx)
            self.ctx = ctx
        except EnableError as e:
            self.error = e
        except Exception as e:                      # noqa: BLE001 - reported, never silent
            self.error = EnableError("enable-not-back", str(e))


def enable_ad9364(ident: PlutoIdentity, runner, iio, data_dir: Path, *, operator_confirmed: bool,
                  watcher_factory: Callable[..., QuietWatcher] = QuietWatcher,
                  now: Callable[[], float] = time.time) -> EnableResult:
    """Run steps 1-5 (module docstring) and return what happened. `runner`
    is a guard.SshRunner (run, detach); `iio` the libiio module; the backup
    goes under `data_dir`/pluto-env. Raises EnableError with a reason from
    REASONS; a refusal changes nothing."""
    if not ident.can_enable_ad9364:
        raise EnableError("enable-firmware", f"{ident.family} ({ident.fw_version or 'no version'})")
    if ident.chip in ("ad9364", "ad9361"):
        raise EnableError("enable-done", ident.chip)
    if not operator_confirmed:
        raise EnableError("enable-unconfirmed")
    steps = []
    try:
        status, env_text = runner.run("fw_printenv")
    except Exception as e:                          # noqa: BLE001
        raise EnableError("enable-ssh", str(e)) from None
    if status != 0:
        raise EnableError("enable-ssh", env_text.strip()[:200])
    key = (ident.key or "unknown").replace(":", "-")
    folder = Path(data_dir) / "pluto-env"
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / f"{key}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now()))}.env"
    backup.write_text(env_text)
    steps.append(f"backed up to {backup}")
    for name, value in SETENV:
        status, out = runner.run(f"fw_setenv {name} {value}")
        if status != 0:
            raise EnableError("enable-ssh", f"fw_setenv {name}: {out.strip()[:200]}")
        steps.append(f"fw_setenv {name} {value}")
    status, after_text = runner.run("fw_printenv")
    env = parse_env(after_text)
    if status != 0 or any(env.get(n) != v for n, v in SETENV):
        raise EnableError("enable-verify", ", ".join(f"{n}={env.get(n)}" for n, _v in SETENV))
    steps.append("read back")
    watcher = watcher_factory(iio, ident.uri).start()
    try:
        runner.detach("sleep 1; reboot")
    except Exception as e:                          # noqa: BLE001
        raise EnableError("enable-ssh", f"reboot: {e}") from None
    watcher.armed()
    steps.append("reboot sent")
    watcher.join(GONE_TIMEOUT_S + BACK_TIMEOUT_S + QUIET_HOLD_S + 10)
    result = watcher.result
    result.backup = backup
    result.steps = steps
    if watcher.error is not None:
        raise watcher.error
    after = identify(watcher.ctx, ident.uri, mac=ident.mac)
    result.after = after
    if after.chip != "ad9364" or not after.covers(144.8e6):
        raise EnableError("enable-range", f"{after.chip}, {after.tx_range}")
    return result
