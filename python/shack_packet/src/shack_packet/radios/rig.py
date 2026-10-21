"""A link through a shack_rig `Rig`: what IcomLink and YaesuLink share.

The rig library already owns the radio's protocol, its transports, its
state cache and - above all - its rule that a rig keys only under a lease
it re-asks while keyed (shack_rig.lease). So a rig link keys the rig with a
lease of its own whose `refusal()` is this link's key-time test (the
station's callsign and transmit switch, the claim, the watchdog): the rig
then unkeys within its own poll if this link's supervisor were ever stuck,
which is the second, independent route to an unkey that a packet station
running unattended should have.

Audio goes through the rig's transmit sink (`send_audio`, `tx_end`,
`tx_drain_s`), which the rig refuses unless it is keyed under that lease;
receive audio comes from `listen_audio`, busy from `state["busy"]` (the
rig's squelch read, switched on with `watch_squelch`).

What differs between the two makers is how a call reaches the rig - an
IcomRig lives on an asyncio loop this link runs in a thread of its own; a
YaesuRig is called directly - and how the radio is set up for packet.

**The set-up for data** is shack_rig.datasetup's, with each maker's list
of settings (shack_rig.icom.datasetup, shack_rig.yaesu.datasetup): on open,
first put back anything a session that died left changed (`recover`), then
read, save and set each setting (`apply`); on close - and on a failed
open - put back what was changed (`restore`). With `setup_dir` the saved
values are kept on disk per radio, so a crash is undone by the next open.
The report goes to the app as link events (`setup-report`,
`setup-restored`, `setup-recovered`) and stays on the link
(`setup_report`): "What the app changed on your radio".

**The channel is confirmed, and the radio is only keyed on it** (0.3.2).
An operator can turn the radio's own VFO, or its mode knob, at any time;
the rig's cache follows within a poll (an IC-9700 also volunteers the
change as a CI-V transceive frame). `frequency` is the channel the app has
confirmed - tuned at open, or accepted after the operator moved the radio
(`confirm_channel`) - and the link refuses to key ("off-channel") while
the radio is anywhere else, or ("off-mode") in a mode other than FM, FM-D
or DATA-FM. The same test is in the lease the rig re-asks while keyed, so
a dial turned mid-over unkeys. Frames meant for one channel therefore
never go out on another before the app has judged it (Packet Radio's
tuning.py does the judging: the band plan, arming, the queue). A
satellite pass that owns the VFOs (`vfo_owner`, set by IcomControl) is
excused: its Doppler steps move them on purpose, and its own gate governs
transmit. `tuned()` is the cache's frequency and mode, for an app to read
without asking the radio.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .base import REASONS, AudioRadioLink, LinkRefused

log = logging.getLogger(__name__)

#: The rig modes a packet link sends in (shack_rig.model.MODES; the DATA flag
#: is separate, so FM-D and DATA-FM are "FM" here).
PACKET_MODES = ("FM",)


class LinkLease:
    """The lease a rig link keys its rig under (module docstring)."""

    def __init__(self, link: RigLink, holder: str = "packet"):
        self.link = link
        self.holder = holder

    def refusal(self) -> str | None:
        why = self.link._lease_refusal()
        return None if why is None else REASONS.get(why, why)


class RigLink(AudioRadioLink):
    """Base for IcomLink and YaesuLink. Subclasses set `self.rig`, implement
    `_call` and the packet set-up, and call `_rig_ready()` once the rig is up."""

    def __init__(self, *, frequency: int | None = None, simplex: bool = True, use_busy: bool = True,
                 transmit: bool = True, setup_dir: str | Path | None = None,
                 mod_level_percent: float | None = None, **kw):
        super().__init__(**kw)
        #: Where each radio's saved settings are kept until they are put
        #: back (shack_rig.datasetup.Journal); None keeps them in memory only.
        self.setup_dir = Path(setup_dir) if setup_dir else None
        #: The transmit audio input's level to set, in percent; None to read
        #: and report it only (no level has been measured on the air yet).
        self.mod_level_percent = mod_level_percent
        #: The set-up's report lines (shack_rig.datasetup.Line), as applied,
        #: as put back at close, and as recovered from a session that died.
        self.setup_report: list = []
        self.setup_restored: list = []
        self.setup_recovered: list = []
        #: [(key, sentence)] about this radio beyond its settings.
        self.setup_warnings: list[tuple[str, str]] = []
        self._data_setup = None
        #: Hz to tune on opening, or None to leave the radio where it is.
        self.frequency = frequency
        self.simplex = simplex
        self.use_busy = use_busy
        #: False for a receive-only station: the rig is not asked for a
        #: transmit path at all, and key() refuses ("receive-only").
        self.transmit = transmit
        self.rig = None
        self.lease = LinkLease(self)
        self._audio_failed = False
        self._stop_audio = None
        #: What the set-up did and could not do, for the app's status page.
        self.setup: dict = {}
        #: Who else is moving the VFOs on purpose ("satellite" during a
        #: pass, IcomControl.begin_pass), or None (module docstring).
        self.vfo_owner: str | None = None

    def _refusal(self) -> str | None:
        return super()._refusal() or self._channel_refusal()

    def _channel_refusal(self) -> str | None:
        """"off-channel" or "off-mode" while the radio is not where the app
        confirmed it (module docstring); None when it is, or when the cache
        does not know yet (the rig refuses a key on an unknown frequency
        itself)."""
        rig = self.rig
        if rig is None or self.frequency is None or self.vfo_owner is not None:
            return None
        state = rig.state
        hz = state.get("freq")
        if hz is not None and int(hz) != int(self.frequency):
            return "off-channel"
        if state.get("band_mode") not in (None, "VFO"):
            # A TM-V71's band put on a memory channel: it transmits on the
            # memory's frequency, while FO (the cache's "freq") still reads
            # the VFO's - so it is not the channel the app confirmed.
            return "off-channel"
        mode = state.get("mode")
        if mode is not None and mode not in PACKET_MODES:
            return "off-mode"
        return None

    def tuned(self) -> dict | None:
        """The radio's frequency and mode as the rig's cache has them now:
        {"freq": Hz or None, "mode": "FM" or None, "data": bool, "label":
        "FM-D", "memory": True when a TM-V71's band is on a memory channel,
        so "freq" (its VFO) is not where it transmits}; None with no rig, or while a satellite pass owns the VFOs.
        Never asks the radio, so any thread may call it."""
        rig = self.rig
        if rig is None or self.vfo_owner is not None:
            return None
        st = rig.state
        return {"freq": st.get("freq"), "mode": st.get("mode"), "data": bool(st.get("data_mode")),
                "label": st.get("mode_label"), "memory": st.get("band_mode") not in (None, "VFO")}

    def confirm_channel(self, hz: int) -> None:
        """The app accepts `hz`, where the operator has put the radio, as
        the channel: keying is allowed there, and a reopen tunes there
        rather than snapping back. Nothing is sent to the radio."""
        if self.frequency is not None and int(hz) != int(self.frequency):
            log.warning("channel confirmed at %.4f MHz (was %.4f)", int(hz) / 1e6, int(self.frequency) / 1e6)
        self.frequency = int(hz)

    def tune_channel(self, hz: int) -> bool:
        """The app moves the radio to `hz` while the link runs (the scope's
        click): through the data set-up, so it is journalled, read back and
        counted as the app's change - put back at close, unlike a channel
        the operator turned to (shack_rig.datasetup rule 6) - and confirmed
        as the channel. Blocking: never from a window's thread."""
        rig = self.rig
        if rig is None:
            return False
        setup = self._data_setup
        s = {x.key: x for x in setup.settings}.get("frequency") if setup is not None else None
        if s is not None:
            line = setup.adjust(s, int(hz))
            ok = line.status == "set"
        else:
            ok = bool(self._call(rig.set_freq, int(hz)))
        if ok:
            self.confirm_channel(int(hz))
        return ok

    def _lease_refusal(self) -> str | None:
        why = self._refusal()
        if why is not None:
            return why
        if not self._keyed and self._keying_since is None:
            # The rig asks this as it keys, not only while keyed. A key that
            # reaches the rig after key() gave up on it (a call that timed
            # out while the rig's loop was busy, then the unkey queued behind
            # it) used to find an armed station and a lease that permitted -
            # and keyed the radio after its unkey. Now the rig refuses it.
            return "not-keyed"
        if self._keyed and self.clock() - self._keyed_at > self.max_key_s:
            return "watchdog"
        return None

    # -- the set-up for data ------------------------------------------------------------

    def _apply_data_setup(self, settings: list, maker: str, where: str) -> None:
        """Recover, then apply `settings` (module docstring). Lines that
        failed are also a "setup" event, as before."""
        from shack_rig.datasetup import DataSetup, Journal, journal_name
        model = getattr(getattr(self.rig, "model", None), "key", "") or "radio"
        name = f"{getattr(getattr(self.rig, 'model', None), 'name', model)} {where}".strip()
        journal = None
        if self.setup_dir is not None:
            journal = Journal(self.setup_dir / journal_name(maker, model, where), radio=name)
        setup = DataSetup(settings, journal, radio=name)
        self._data_setup = setup
        self.setup_recovered = setup.recover()
        if self.setup_recovered:
            self._event("radio", "setup-recovered", {"lines": [ln.public() for ln in self.setup_recovered]})
        self.setup_report = setup.apply()
        self._event("radio", "setup-report", {"lines": [ln.public() for ln in self.setup_report]})
        for line in self.setup_report:
            self.setup[line.key] = line.status in ("set", "already", "set-unrestorable")
        failed = [ln.key for ln in self.setup_report if ln.status in ("failed", "unread")]
        if failed:
            self._event("radio", "setup", {"what": ",".join(failed)})
        for line in self.setup_report:
            if line.status == "manual":
                self.setup[f"{line.key}_note"] = line.note
                self._event("radio", "setup", {"what": line.key, "note": line.note})
        for key, text in self.setup_warnings:
            self._event("radio", "setup-warning", {"what": key, "note": text})

    def _restore_data_setup(self) -> None:
        """Put back what the set-up changed. Where the rig lives, called from
        the link's own shutdown."""
        setup, self._data_setup = self._data_setup, None
        if setup is None:
            return
        self.setup_restored = setup.restore()
        if self.setup_restored:
            self._event("radio", "setup-restored", {"lines": [ln.public() for ln in self.setup_restored]})

    # -- controls turned live (0.3.0's control strip) -------------------------------------
    #
    # Each goes through the data set-up (shack_rig.datasetup.DataSetup.adjust):
    # the value as found is journalled before the first change and put back
    # at close with everything else, and every write is read back. Blocking:
    # call from a worker thread, never a window's.

    def _control_settings(self) -> dict:
        """{key: shack_rig.datasetup.Setting} this radio can turn; none here."""
        return {}

    def _read_meters(self, transmitting: bool) -> dict:
        return {}

    def control_keys(self) -> list[str]:
        if self.rig is None:
            return []
        return list(self._control_settings())

    def control_range(self, key: str) -> int:
        """The top of a level control's raw scale (an Icom's 0-255)."""
        return 255

    def control_choices(self, key: str):
        """[(raw, name)] for a control chosen by name (a TM-V71's power
        H/M/L), None for a level."""
        return None

    def control_writable(self, key: str) -> bool:
        """False for a control the app only reads (a TM-V71's squelch)."""
        return True

    def read_control(self, key: str):
        """The raw level now (0-255 on an Icom), or None."""
        s = self._control_settings().get(key) if self.rig is not None else None
        if s is None:
            return None
        from shack_rig.datasetup import UNAVAILABLE
        got = s.read()
        return None if got == UNAVAILABLE else got

    def set_control(self, key: str, raw):
        """Set a control (block comment above); the datasetup.Line, or None
        when this radio has no such control or no link."""
        s = self._control_settings().get(key) if self.rig is not None else None
        if s is None:
            return None
        from shack_rig.datasetup import DataSetup
        if self._data_setup is None:
            self._data_setup = DataSetup([], None, radio="radio")
        line = self._data_setup.adjust(s, raw)
        self._event("radio", "control", {"key": key, "line": line.public()})
        return line

    def read_meters(self) -> dict:
        """The radio's meters now (shack_rig.icom.controls.read_meters):
        signal while receiving, Po/SWR/ALC while keyed; {} without them."""
        if self.rig is None:
            return {}
        return self._read_meters(bool(getattr(self, "_keyed", False)))

    # -- how a call reaches the rig ---------------------------------------------------

    def _call(self, fn, *args, timeout: float = 2.0):
        """Run `fn(*args)` where the rig lives and return its result."""
        raise NotImplementedError

    def _post(self, fn, *args) -> None:
        """Run `fn(*args)` where the rig lives, in order, without waiting."""
        self._call(fn, *args)

    # -- keying -------------------------------------------------------------------------

    def _radio_key(self) -> None:
        from shack_rig import PttRefused
        rig = self.rig
        if not self.transmit:
            raise LinkRefused("receive-only")
        if rig is None or not rig.state.get("connected"):
            raise LinkRefused("disconnected")
        if not rig.tx_rate or not rig.tx_audio_ready:
            raise LinkRefused("radio-refused", "the rig has no transmit audio path")
        if rig.tx_rate != self.rate:
            raise LinkRefused("radio-refused", f"the rig's transmit audio is {rig.tx_rate} Hz, not {self.rate}")
        self._audio_failed = False
        try:
            ok = self._call(rig.set_ptt, True, self.lease)
        except PttRefused as e:
            raise LinkRefused("radio-refused", e.reason) from None
        if not ok:
            raise LinkRefused("radio-refused", "the key did not reach the radio")

    def _radio_unkey(self) -> None:
        rig = self.rig
        if rig is None:
            return
        if not self._call(rig.set_ptt, False):
            # IcomRig and YaesuRig both keep an owed unkey and send it the
            # moment the link is back; this link retries too (base.py).
            raise OSError("the unkey did not reach the radio")

    def _radio_audio(self, pcm: bytes) -> bool:
        if self._audio_failed:
            return False
        rig = self.rig

        def send() -> None:
            if not rig.send_audio(pcm):
                self._audio_failed = True

        self._post(send)
        return True

    def _radio_audio_end(self) -> None:
        self._post(self.rig.tx_end)

    def _radio_drain_s(self) -> float:
        try:
            return float(self.rig.tx_drain_s)
        except Exception:                           # noqa: BLE001
            return 0.0

    def _radio_alive(self) -> bool:
        rig = self.rig
        return rig is not None and bool(rig.state.get("connected"))

    # Every unkey is confirmed by asking the radio (base.py): the rig's own
    # set_ptt(False) reports success once the command is handed to its
    # transport, which is not the radio having acted on it.
    CONFIRMS_UNKEY = True

    def _radio_ptt_readback(self) -> bool | None:
        raise NotImplementedError

    def _radio_busy_now(self) -> bool | None:
        if not self.use_busy or self.rig is None:
            return None
        return self.rig.state.get("busy")

    # -- receive ------------------------------------------------------------------------

    def _rig_ready(self) -> None:
        """Receive audio to the engine, busy watched."""
        rig = self.rig
        if self.use_busy:
            self._call(rig.watch_squelch, True)
        self._stop_audio = self._call(rig.listen_audio, self._rx)

    def _rig_release(self) -> None:
        stop, self._stop_audio = self._stop_audio, None
        if stop is not None:
            try:
                self._call(stop)
            except Exception:                       # noqa: BLE001
                log.exception("unsubscribing audio")


def sdr_hub_warnings(audio_device: str) -> list[tuple[str, str]]:
    """A warning when an SDR shares a USB hub with the radio's sound device
    (shack_rig.usbplace); none when there is none or the system cannot say."""
    from shack_rig import usbplace
    found = usbplace.sdrs_sharing(audio_device)
    if not found:
        return []
    return [("usb-hub-sdr", "the radio's USB sound device shares a USB hub with " + ", ".join(sorted(set(found)))
             + ": a busy SDR on the same hub can starve the radio's audio; give the radio a USB port of its "
               "own if packet over USB fails")]


def wait_for(cond, timeout: float, step: float = 0.05) -> bool:
    """Block until cond() or the timeout; for set-up only, never while keyed."""
    done = threading.Event()
    end = timeout
    while end > 0:
        if cond():
            return True
        done.wait(step)
        end -= step
    return bool(cond())
