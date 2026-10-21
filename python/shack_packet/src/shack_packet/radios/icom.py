"""An Icom as a packet radio: IC-9700 over its LAN or USB, IC-705 over USB
or Wi-Fi (the same LAN protocol).

Through shack_rig's IcomRig, built by `make_rig` from the same profile the
Icom app stores (model, transport, host/port/username or civ_device/
audio_device...), so everything that app learned about reaching an Icom -
the LAN handshake and its wedges, the USB probe, the CI-V pacing on a
cable, the owed unkey - is this link's too. The rig lives on an asyncio
loop this link runs in a thread of its own; every call into it is made on
that loop (`_call`), because IcomRig and its UDP streams are not
thread-safe and are not meant to be.

**What opening does to the radio** is shack_rig.icom.datasetup's list, read,
saved, set and put back on close by shack_rig.datasetup (rig.py says how):
the frequency, FM-D on the model's filter (0x26, one frame, so the radio
never sits in plain FM with the microphone live), simplex, every tone off,
NR/NB/auto notch/COMP/VOX/MONI/AFC off, DATA MOD to the transport's source
(LAN for the network, USB for the cable), DATA OFF MOD to MIC, the
transport's MOD level (reported; set only when a level is given), its AF SQL
off, and the squelch level read. Anything the model's table does not source
(the IC-705's menu items) is reported as one for the operator to set. Then
the squelch is read as busy (15 01) and receive audio goes to the engine.
None of it on connect alone.

**Audio** is 12 kHz both ways, which is what the LAN stream is asked for
and what the USB path divides to and up from exactly (shack_rig.usbaudio,
txaudio). On USB the transmit sink is `aplay` on the appliance and
PortAudio on a desktop (profile `tx_audio`); the link sets it from the
profile, `portaudio` unless the profile says otherwise.

**Turnaround** is assumed 0.15 s until measured (turnaround.py): Ray's
IC-9700 reached half power 65 ms after a CI-V key with audio already
playing (openhamshack-nb docs/ic9700-research.md, 12/10/2026), plus a CI-V
round trip over the LAN and a margin. Nobody has measured a 705's.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from shack_rig.icom import civ

from .base import LinkRefused
from .rig import RigLink, sdr_hub_warnings, wait_for

log = logging.getLogger(__name__)


class IcomLink(RigLink):
    """`profile` is a shack_rig.icom.make_rig profile; `password` the LAN
    password. `rig` may be handed in (a test's, or an app's own) and is then
    started and stopped here all the same."""

    DEFAULT_TURNAROUND_S = 0.15
    CONNECT_S = 20.0

    def __init__(self, profile: dict | None = None, *, password: str = "", data_source: str | None = None,
                 rig=None, connect_s: float | None = None, minimum_power_for_test: bool = False, **kw):
        super().__init__(**kw)
        self.profile = dict(profile or {})
        self.password = password
        #: The real-radio dummy-load test's "lowest power" step (GUIDE.md,
        #: shack-packet HANDOVER.md "What's next"): off unless asked, either
        #: here or in the profile. See `_ensure_minimum_power`.
        self.minimum_power_for_test = bool(minimum_power_for_test
                                           or self.profile.get("minimum_power_for_test"))
        #: The RF POWER level as first read this session, for
        #: `_restore_power_step`; None until the minimum-power step has run,
        #: and again once it has been restored - so a link that never keyed,
        #: or never had the setting on, never writes a level back.
        self._power_saved_level: int | None = None
        #: Whether the minimum-power step has already run and confirmed 0
        #: this session, so the second and later keys of an armed session
        #: do not each cost a read, a write and a read-back.
        self._power_min_confirmed = False
        #: The minimum-power step's own sentence, the last time it refused -
        #: None once it has not failed, or has not been tried yet. For the
        #: app (links.power_status): there is nowhere else it can learn why
        #: a key was refused for this particular reason.
        self._power_min_failed: str | None = None
        transport = str(self.profile.get("transport") or "lan").lower()
        #: DATA MOD wanted: the network for LAN/Wi-Fi, the codec for USB.
        self.data_source = data_source or ("usb" if transport == "usb" else "lan")
        if transport == "usb":
            self.profile.setdefault("tx_audio", "portaudio" if self.profile.get("tx_audio_device") else "alsa")
        self.profile["audio_rate"] = 12000
        self.rate = 12000
        self._given_rig = rig
        self.connect_s = self.CONNECT_S if connect_s is None else connect_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    # -- the loop the rig lives on --------------------------------------------------

    def _start_loop(self) -> None:
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._loop = loop
        self._loop_thread = threading.Thread(target=run, name="icom-link loop", daemon=True)
        self._loop_thread.start()
        ready.wait(5)

    def _stop_loop(self) -> None:
        loop, self._loop = self._loop, None
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
        loop.close()

    def _call(self, fn, *args, timeout: float = 2.0):
        loop = self._loop
        if loop is None:
            raise OSError("the radio's loop is not running")

        async def run():
            got = fn(*args)
            if asyncio.iscoroutine(got):
                got = await got
            return got

        return asyncio.run_coroutine_threadsafe(run(), loop).result(timeout)

    def _post(self, fn, *args) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(fn, *args)

    # -- lifetime -----------------------------------------------------------------------

    def _open_radio(self) -> None:
        from shack_rig.icom import make_rig
        self._start_loop()
        try:
            if self._given_rig is not None:
                self.rig = self._given_rig
            else:
                self.rig = self._call(lambda: make_rig(self.profile, password=self.password,
                                                       tx_enabled=self.transmit, poll_interval=0.1, scope=False))
            self._call(self.rig.start)
            from shack_rig.icom.lan import AUTH_REJECTED
            # A refused login ends the wait at once and says so by its own
            # reason: the rig does not try again by itself (shack_rig
            # IcomRig._supervise), so the app asks the operator for the
            # password instead of waiting out CONNECT_S on a radio that has
            # already said no.
            if not wait_for(lambda: (self.rig.state.get("connected") and self.rig.state.get("freq"))
                            or self.rig.state.get("error") == AUTH_REJECTED, self.connect_s) \
                    or self.rig.state.get("error") == AUTH_REJECTED:
                why = self.rig.state.get("error") or "the radio did not answer"
                raise LinkRefused("radio-auth" if why == AUTH_REJECTED else "radio-error", str(why))
            self._prepare()
            self._rig_ready()
        except Exception:
            self._shutdown_rig()
            raise

    def _prepare(self) -> None:
        from shack_rig.icom import datasetup
        rig = self.rig
        transport = str(self.profile.get("transport") or "lan").lower()
        level = None
        if self.mod_level_percent is not None:
            level = datasetup.mod_level_from_percent(self.mod_level_percent)
        settings = datasetup.settings(rig, self._call, source=self.data_source,
                                      frequency=int(self.frequency) if self.frequency else None,
                                      mod_level=level, simplex=self.simplex)
        self.setup_warnings = datasetup.warnings(rig.model, transport, self.transmit)
        if transport == "usb":
            self.setup_warnings += sdr_hub_warnings(self.profile.get("audio_device") or "")
        where = self.profile.get("host") if transport != "usb" else (self.profile.get("civ_device") or "usb")
        self._apply_data_setup(settings, "icom", str(where or "radio"))

    def _control_settings(self) -> dict:
        from shack_rig.icom import controls
        if getattr(self, "_controls", None) is None:
            self._controls = controls.settings(self.rig, self._call)
        return self._controls

    def _read_meters(self, transmitting: bool) -> dict:
        from shack_rig.icom import controls
        return controls.read_meters(self.rig, self._call, transmitting)

    def _close_radio(self) -> None:
        self._controls = None
        self._shutdown_rig()

    def _shutdown_rig(self) -> None:
        rig = self.rig
        if rig is not None and self._loop is not None:
            self._rig_release()
            try:
                self._call(lambda: rig.set_ptt(False))
            except Exception:                       # noqa: BLE001 - closing regardless
                log.exception("unkeying the radio")
            # From this thread, not the loop: each restore waits on the loop
            # for the radio's answer that confirms it.
            try:
                self._restore_data_setup()
            except Exception:                       # noqa: BLE001
                log.exception("putting the radio's settings back")
            for step in (lambda: rig.watch_squelch(False), self._restore_power_step):
                try:
                    self._call(step)
                except Exception:                   # noqa: BLE001 - closing regardless
                    log.exception("putting the radio back")
            try:
                self._call(rig.stop, timeout=10)
            except Exception:                       # noqa: BLE001
                log.exception("stopping the rig")
        self._stop_loop()

    # -- the real-radio test's "lowest power" step ---------------------------
    #
    # Added for the IC-9700 dummy-load test (shack-packet HANDOVER.md "What's
    # next"). While `minimum_power_for_test` is on, the first key of an armed
    # session reads RF POWER (14 0A), sets it to 0000 and reads it back before
    # any key reaches the radio; a missing answer or a readback that is not 0
    # refuses the key outright (LinkRefused, no set_ptt sent at all). The
    # level is put back - and the step allowed to run again - at close and
    # when the station disarms mid-transmission (_abort_locked below); never
    # on an ordinary end-of-over unkey, or every single transmission in the
    # test would cost two extra CI-V round trips for a setting the radio
    # keeps by itself.
    #
    # 0000 is not nought watts: the IC-9700's floor is roughly 0.5 W (Icom's
    # manual; see GUIDE.md's ALC/Po note), so a dummy load is required at
    # every level this sets, 0000 included.

    def _ensure_minimum_power(self) -> None:
        if not self.minimum_power_for_test or self._power_min_confirmed:
            return
        try:
            self._power_min_failed = None
            self._ensure_minimum_power_or_raise()
        except LinkRefused as e:
            # Kept for the app (links.power_status): a key refused for this
            # reason is otherwise indistinguishable, from outside this class,
            # from any other "radio-refused".
            self._power_min_failed = e.detail or e.reason
            raise

    def _ensure_minimum_power_or_raise(self) -> None:
        rig = self.rig
        if rig is None:
            raise LinkRefused("radio-refused", "RF power level: no rig to ask - refusing to key")
        before = self._call(rig.ask, civ.read_rf_power(), b"\x14\x0a", 2.0, timeout=3.0)
        if before is None:
            raise LinkRefused("radio-refused",
                              "RF power level: the radio did not answer the pre-test read - refusing to key")
        try:
            saved = civ.decode_rf_power(before)
        except civ.CivError as e:
            raise LinkRefused("radio-refused",
                              f"RF power level: unreadable answer ({e}) - refusing to key") from None
        if not self._call(rig.send, civ.set_rf_power(0)):
            raise LinkRefused("radio-refused",
                              "RF power level: the minimum-power set did not reach the radio - refusing to key")
        after = self._call(rig.ask, civ.read_rf_power(), b"\x14\x0a", 2.0, timeout=3.0)
        if after is None:
            raise LinkRefused("radio-refused",
                              "RF power level: no answer confirming minimum power - refusing to key "
                              "(a dummy load is required; the 9700's 0% is still roughly 0.5W, not zero)")
        got = civ.decode_rf_power(after)
        if got != 0:
            raise LinkRefused("radio-refused",
                              f"RF power level: the radio reports {got}, not 0, after the minimum-power "
                              f"set - refusing to key")
        self._power_saved_level = saved
        self._power_min_confirmed = True
        log.info("RF power level: minimum confirmed for this test session (was %d, now 0; "
                 "a dummy load is required - the 9700's 0%% is still roughly 0.5W, not zero)", saved)

    def _radio_key(self) -> None:
        self._ensure_minimum_power()
        super()._radio_key()

    def _restore_power_step(self) -> None:
        """On the rig's own loop: put RF POWER back as first read, and say
        both values. Nothing to do if the level was never touched - the
        test setting was off, or the minimum-power step never got this far."""
        saved, self._power_saved_level = self._power_saved_level, None
        self._power_min_confirmed = False
        if saved is None:
            return
        rig = self.rig
        if rig is None:
            return
        rig.send(civ.set_rf_power(saved))
        log.info("RF power level: restored to %d (was held at 0 for the minimum-power test step)", saved)

    # -- the unkey's confirmation and the way round a stuck loop --------------

    #: How long the emergency unkey gives the rig's loop before writing to a
    #: USB radio's serial port itself.
    EMERGENCY_LOOP_S = 0.3

    def _radio_ptt_readback(self) -> bool | None:
        rig = self.rig
        if rig is None:
            return None
        got = self._call(rig.ask, civ.read_ptt(), b"\x1c\x00", 0.5, timeout=1.0)
        if got is None or len(got) < 3:
            return None
        return got[2] != 0

    def _emergency_unkey(self) -> None:
        """The unkey from outside the rig's loop (base.py): the hard
        watchdog's, and a process's on its way out. Posted to the loop like
        any other call; if the loop has not run it within EMERGENCY_LOOP_S -
        it is stuck, in a sound device's call, say - and the radio is on a
        USB cable, the unkey frame is written to the serial port directly.
        The loop's own writer cannot be writing at the same moment: it runs
        on that stuck loop. Over the LAN there is no such way round (the
        stream's sequence numbers live on the loop), so the post is all
        there is."""
        rig, loop = self.rig, self._loop
        if rig is None:
            return
        ran = threading.Event()

        def unkey() -> None:
            ran.set()
            rig.set_ptt(False)

        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(unkey)
            except RuntimeError:                    # closed between the test and the call
                pass
            if ran.wait(self.EMERGENCY_LOOP_S):
                return
        from shack_rig.serialio import SerialPort
        port = getattr(getattr(rig, "_link", None), "_port", None)
        if isinstance(port, SerialPort):
            frame = civ.frame(civ.set_ptt(False), to=rig.radio_addr, frm=rig.ctrl_addr)
            ok = port.write(frame)
            log.error("the radio's loop did not run the unkey in %.1f s; wrote it to %s directly (%s)",
                      self.EMERGENCY_LOOP_S, port.device, "sent" if ok else "failed")
        else:
            log.error("the radio's loop did not run the unkey in %.1f s, and over this transport there is no "
                      "other way to the radio; it stays posted", self.EMERGENCY_LOOP_S)

    def _abort_locked(self, reason, events=None, detail=None) -> None:
        if reason == "disarmed":
            self._post(self._restore_power_step)
        super()._abort_locked(reason, events, detail)
