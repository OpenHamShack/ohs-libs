"""The rest of an IC-9700 that an app wants beside packet: its spectrum
scope, its RF POWER level and Po meter, and a satellite pass's VFOs - from
any thread, through an open IcomLink (Packet Radio 0.3.0, workstream F).

    ctl = IcomControl(link)              # link: an open radios.IcomLink
    stop = ctl.listen_scope(fn)          # fn(spectrum) on the rig's loop
    ctl.set_rf_power(64); ctl.rf_power()
    ctl.begin_pass("split", rx_hz=..., tx_hz=...); ctl.retune(...); ctl.end_pass()

A façade rather than more methods on IcomLink: the link is the packet
station's transmit path, and its safety core (radios/base.py) is easier to
read with nothing else in the file. Every call here goes through the link's
own `_call`, onto the loop the rig lives on (IcomRig is not thread-safe),
and none of them keys anything.

**The scope** is the rig's own (shack_rig IcomRig: the 27 10/27 11 keeper,
the wave-data assembler, the settings cache). The link makes its rig with
`scope=False`, so the keeper leaves the radio's CI-V scope output alone
until `listen_scope` switches the rig's scope on; the keeper then turns the
radio's output on while somebody listens and off SCOPE_RELEASE_S after the
last listener leaves, which is how a hidden scope page stops costing the
LAN anything. A link that cannot carry a scope (USB at 19200 baud) says so
in `scope_refusal()`, the rig's own sentence.

**RF POWER** is a plain read and write of 14 0A (0-255). Nothing here puts
it back by itself: the caller that changes it - the app's adaptive power -
keeps the level it found and restores it, and logs every change.

**A satellite pass** is shack_rig.icom.satdd's set-up through
shack_rig.datasetup, journalled beside the link's own packet set-up so a
crash mid-pass is undone at the next pass or `recover()`. End the pass
before the link closes: the link's own restore puts the frequency back,
and the pass's VFOs must be back first.
"""

from __future__ import annotations

import logging
from typing import Callable

from shack_rig.icom import civ

log = logging.getLogger(__name__)


class IcomControlError(Exception):
    """A request the radio did not carry out; `reason` is a key the app
    translates, `detail` the radio's or library's own words."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class IcomControl:
    ASK_S = 1.0

    def __init__(self, link):
        self.link = link
        self._pass = None
        self._pass_how: str | None = None

    # -- plumbing -----------------------------------------------------------------

    @property
    def rig(self):
        return getattr(self.link, "rig", None)

    def available(self) -> bool:
        """Whether there is an open link with a rig to talk to."""
        return self.rig is not None and bool(getattr(self.link, "is_open", False)) \
            and getattr(self.link, "_loop", None) is not None

    def _call(self, fn, *args, timeout: float = 2.0):
        if not self.available():
            raise IcomControlError("not-open")
        return self.link._call(fn, *args, timeout=timeout)

    def _ask(self, command: bytes, key: bytes) -> bytes | None:
        rig = self.rig
        return self._call(lambda: rig.ask(command, key, self.ASK_S), timeout=self.ASK_S + 2)

    def state(self) -> dict:
        """A copy of the rig's state cache (freq, scope_settings...)."""
        rig = self.rig
        return dict(rig.state) if rig is not None else {}

    # -- the scope ----------------------------------------------------------------

    def scope_refusal(self) -> str | None:
        """Why this link cannot carry a scope, or None."""
        rig = self.rig
        if rig is None:
            return "not-open"
        try:
            return self._call(lambda: rig.scope_stats().get("refused"))
        except Exception:                           # noqa: BLE001 - asked only to say why
            return None

    def listen_scope(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """Hand every whole spectrum to `fn` (called on the rig's loop: keep
        it short, copy and return). Returns the call that stops it."""
        rig = self.rig

        def start():
            rig.scope = True
            return rig.listen_scope(fn)

        stop = self._call(start)

        def unlisten() -> None:
            try:
                self._call(stop)
            except Exception:                       # noqa: BLE001 - the link went first
                log.debug("scope listener already gone with the link")
        return unlisten

    def scope_settings(self) -> dict | None:
        return self.state().get("scope_settings")

    def set_scope(self, **settings) -> bool:
        """Any of span_hz, mode, edge, ref_db, speed, vbw, averaging
        (IcomRig.set_scope_settings); False if refused or not sent."""
        rig = self.rig
        return bool(self._call(lambda: rig.set_scope_settings(settings)))

    def tune(self, hz: int) -> bool:
        rig = self.rig
        return bool(self._call(rig.set_freq, int(hz)))

    # -- power --------------------------------------------------------------------

    def rf_power(self) -> int | None:
        """RF POWER now, 0-255, or None if the radio did not answer."""
        got = self._ask(civ.read_rf_power(), b"\x14\x0a")
        if got is None:
            return None
        try:
            return civ.decode_rf_power(got)
        except civ.CivError:
            return None

    def set_rf_power(self, level: int) -> bool:
        """Write RF POWER and read it back: True only when the radio says
        the new level."""
        level = max(0, min(255, int(level)))
        rig = self.rig
        if not self._call(rig.send, civ.set_rf_power(level)):
            return False
        return self.rf_power() == level

    def po(self) -> int | None:
        """The Po meter, raw 0-255 (civ.py: 0143 is 50%, 0213 100%)."""
        got = self._ask(civ.read_power(), b"\x15\x11")
        if got is None or len(got) < 4:
            return None
        try:
            return civ.decode_meter(got[2:4])
        except civ.CivError:
            return None

    # -- a satellite pass -----------------------------------------------------------

    def _journal(self, name: str):
        from shack_rig.datasetup import Journal, journal_name
        d = getattr(self.link, "setup_dir", None)
        if d is None:
            return None
        where = str(getattr(self.link, "profile", {}).get("host") or "radio")
        model = getattr(getattr(self.rig, "model", None), "key", "") or "radio"
        return Journal(d / journal_name("icom", f"{model}-{name}", where), radio=f"{model} {where} {name}")

    def recover(self) -> list:
        """Put back what a pass that died left on the radio (its journal)."""
        from shack_rig.datasetup import DataSetup
        from shack_rig.icom import satdd
        journal = self._journal("satellite")
        if journal is None or not journal.load():
            return []
        lines = []
        for how in satdd.HOWS:
            # Either list knows the keys a journal of its kind holds; the
            # frequencies given here are never written (recover writes
            # the journal's values).
            settings = satdd.satellite_settings(self.rig, self._call, how, rx_hz=0, tx_hz=0)
            lines += DataSetup(settings, journal).recover()
            if not journal.load():
                break
        return lines

    @property
    def pass_active(self) -> bool:
        return self._pass is not None

    def begin_pass(self, how: str, *, rx_hz: int, tx_hz: int) -> list:
        """Split or satellite mode on, the VFOs at the first corrected
        frequencies; returns the set-up's report lines. Raises
        IcomControlError("satellite-setup") if a setting the pass needs was
        not set (and puts back what was)."""
        from shack_rig.datasetup import DataSetup
        from shack_rig.icom import satdd
        if self._pass is not None:
            self.end_pass()
        self.recover()
        setup = DataSetup(satdd.satellite_settings(self.rig, self._call, how, rx_hz=rx_hz, tx_hz=tx_hz),
                          self._journal("satellite"), radio="satellite pass")
        # The pass moves the VFOs from here on (rig.py's channel guard
        # stands aside for it; the pass gate governs transmit instead).
        self.link.vfo_owner = "satellite"
        report = setup.apply()
        self._pass, self._pass_how = setup, how
        bad = [ln.key for ln in report if ln.status in ("failed", "unread", "unavailable", "report")]
        if bad:
            self.end_pass()
            raise IcomControlError("satellite-setup", ", ".join(bad))
        return report

    def retune(self, *, rx_hz: int | None = None, tx_hz: int | None = None) -> bool:
        """One Doppler step during a pass (satdd.retune)."""
        from shack_rig.icom import satdd
        if self._pass is None:
            return False
        rig, how = self.rig, self._pass_how
        return bool(self._call(lambda: satdd.retune(rig, how, rx_hz=rx_hz, tx_hz=tx_hz)))

    def end_pass(self) -> list:
        """Put the radio back as the pass found it; the lines restored."""
        setup, self._pass, self._pass_how = self._pass, None, None
        if setup is None:
            return []
        try:
            return setup.restore()
        except IcomControlError:
            log.error("the radio went before the satellite pass's settings were put back; "
                      "they are journalled and are put back at the next pass")
            return []
        finally:
            # After the VFOs are back: the channel guard judges the radio again.
            if getattr(self.link, "vfo_owner", None) == "satellite":
                self.link.vfo_owner = None
