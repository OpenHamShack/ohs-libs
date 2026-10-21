"""An Icom, reached over whatever `make_link` builds: the first `Rig` backend.

Moved from OpenHamShack Icom's `app/radio.py`, where it was `Radio`; the name
`Radio` is kept as an alias so the app's adoption is an import change. The one
behaviour that changed on the way is transmit, and it changed on purpose: see
`set_ptt` and `shack_rig.lease`. Everything else - the poll loop, the
supervisor, the cache, the scope keeper - is the app's, unchanged.

Everything above this reads state from here rather than asking the radio
directly. That is deliberate. WSJT-X polls frequency several times a second
and a control deck polls harder still; sending each of those down the CI-V
link would fill it with questions and leave no room for answers. So one poll
loop keeps the values current and everyone else reads the cache.

The radio also volunteers: turn the dial and an IC-9700 emits a frequency frame
nobody asked for. Those arrive on the same path and update the same cache,
which is why a dashboard follows the tuning knob without polling faster.

**What carries the bytes is not this file's business.** A rig is given a way
of making a `Link` - Icom's LAN protocol, or a serial port - and beyond
starting it, stopping it and pushing frames down it, nothing here knows which
of them it got. The contract is written out in `link.py`, with the reason the
seam sits exactly here and not one layer up or down.

**And this class is the `Rig` surface** (`shack_rig.rig.Rig`), which is the
other half of the same idea. `connect` and `disconnect` are not `start` and
`stop`: `start` and `stop` are the object's lifetime, and `connect` and
`disconnect` are the operator's intention about the link inside it, which the
supervisor then obeys. Keeping them apart is what lets a disconnected radio
still be a radio: it stays in the payload, still answers rigctl, and says
plainly that it is down and why.

`send` and `ask` - the two CI-V-shaped methods - are for this file and for
tests. Nothing above a backend forms a CI-V frame; that is the property that
lets a second maker's rig stand beside this one without rewriting the apps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .. import lease as txlease
from ..i18n import _
from ..lease import PttRefused, TxLease
from ..model import DATA_CAPABLE
from . import civ
from .lan import AUTH_REJECTED

log = logging.getLogger("shack_rig.icom")

#: `scope_settings` before the radio has answered anything.
SCOPE_SETTINGS_BLANK = {"receiver": 0, "mode": None, "span_hz": None, "edge": None,
                        "ref_db": None, "speed": None, "vbw": None, "averaging": None}

#: `dv` once the radio has reported anything in DV. The call signs are as the
#: radio gives them, trimmed: `caller` and its note `suffix` (the "/xxxx"
#: after a D-STAR call sign), `ur` the station called (CQCQCQ for a general
#: call), `rpt1` the repeater the caller went into, `rpt2` its gateway or
#: link. `message` is the caller's 20-character text.
DV_BLANK = {"caller": "", "suffix": "", "ur": "", "rpt1": "", "rpt2": "", "message": "",
            "receiving": False, "packet_loss": False, "heard_at": None}


def _can_data(model, mode: str | None) -> bool:
    """Whether the data flag means anything for this mode on this radio.

    Falls back to the protocol-wide list when there is no model - a Radio can
    be built before the settings that name one have been read, and answering
    "no data anywhere" in that window would look like a radio that cannot do
    it rather than a module that has not asked yet.
    """
    if not mode:
        return False
    if model is not None:
        return model.has_data_mode(mode)
    return mode in DATA_CAPABLE


class IcomRig:
    """One Icom, reached over whatever `make_link` builds.

    `make_link` is a factory rather than a link, and that is deliberate: the
    supervisor below rebuilds the connection from nothing every time the radio
    goes away, and a link reused across that would carry a session the radio
    has already forgotten - sequence numbers and a retransmit buffer on the LAN,
    a file descriptor for a port that has been unplugged on USB. Every attempt
    starts with a new one, exactly as it did when this file built the control
    stream itself.

    `where` is what to call this radio before a link exists to ask, so that a
    profile with nothing filled in can still be logged about by name.
    """

    def __init__(
        self,
        make_link: Callable[[], Any],
        where: str = "",
        model=None,
        radio_addr: int | None = None,
        ctrl_addr: int = civ.DEFAULT_CTRL_ADDR,
        poll_interval: float = 0.25,
        allow_ptt: bool = False,
        audio_rate: int = 0,
        scope: bool = True,
    ):
        self._make_link = make_link
        self._where = where
        self.model = model
        # An explicit address wins; otherwise the model's, which is the
        # factory default for that radio. Both are settable on the radio
        # itself, and a wrong one is silence rather than an error.
        if radio_addr is None:
            radio_addr = model.civ_address if model is not None else civ.DEFAULT_RADIO_ADDR
        self.radio_addr = radio_addr
        self.ctrl_addr = ctrl_addr
        self.poll_interval = poll_interval
        self.allow_ptt = allow_ptt
        self.audio_rate = audio_rate
        self.scope = scope

        self.state: dict[str, Any] = {
            "connected": False,
            "error": None,
            # Whether the operator wants a link to this radio at all, and the
            # one word that answers "what is happening" - see `_link_word`.
            # `connected` on its own cannot: a radio that is switched off and
            # one that has been put down on purpose are both not-connected,
            # and only one of them is something to go and fix.
            "wanted": True,
            "link_state": "connecting",
            # The link is up and the radio says nothing: it is in standby.
            # An Icom with network power-on enabled keeps its LAN side
            # awake - login, pings, an open CI-V stream - while the radio
            # itself answers no CI-V at all, so "connected" alone reads as
            # a radio that is there and frozen. This is the word that tells
            # a person to press Power on rather than to go and fix a cable.
            "asleep": False,
            "radio": None,
            "freq": None,
            "mode": None,
            # The data flag, and the two together as one name. The page shows
            # mode_label: "USB" and "USB-D" are different things to a radio and
            # to an operator, and only one of them will work FT8, so the
            # difference has to be visible without reading a second field.
            "data_mode": False,
            "mode_label": None,
            "filter": None,
            "ptt": False,
            "vfo": "VFOA",
            "split": False,
            "smeter": None,
            "smeter_db": None,
            # The receive front end, read back from the radio rather than
            # remembered from what was asked: the radio keeps both per band,
            # so changing band changes them with nobody touching either.
            # None until the first answer. `preamp_ext` is the IC-9700's
            # external preamp, which shares the preamp byte; None on a radio
            # whose byte does not carry one.
            "preamp": None,
            "preamp_ext": None,
            # The same byte as one whole setting, an index into the model's
            # preamp_mode_names(): on an IC-9700 0 OFF, 1 P.AMP, 2 EXT,
            # 3 P.AMP+EXT; elsewhere the level. `preamp` and `preamp_ext` stay
            # for the clients written before it.
            "preamp_mode": None,
            # Whether the radio's own menu has an external preamp enabled on
            # each band ({"144": True, "430": False, "1200": None}, None until
            # it answers); None on a radio without the setting. Read from the
            # radio, never written by this module - it is the mast-head
            # preamp's DC supply. See models.IC9700_EXT_PREAMP_MENU.
            "ext_preamp_menu": (
                {row[0]: None for row in model.ext_preamp_menu}
                if getattr(model, "ext_preamp_menu", ()) else None),
            "att": None,
            # The radio's CW pitch in Hz (14 09: 300-900 in 5 Hz steps),
            # the tone a CW signal is heard at; read once a second while in
            # CW or CW-R, None until then. An Icom key of its own (rig.py
            # lets a backend add keys), for the app's CW decoder.
            "cw_pitch": None,
            "power": None,
            "swr": None,
            "updated": 0.0,
            "audio": False,
            "audio_rate": None,
            "scope": False,
            "scope_points": None,
            # The scope's own settings as the radio reports them (27 14-1D,
            # 1A 05 0192): None on a model with no scope settings here, and
            # each value None until answered. See `set_scope_settings`.
            "scope_settings": (dict(SCOPE_SETTINGS_BLANK)
                               if getattr(model, "scope", None) else None),
            # D-STAR: the last call heard while in DV (20 00/01/02), None
            # until the radio has reported one. `heard_at` moves on each new
            # over, which is what tells a client a call began.
            "dv": None,
            # The radio's squelch as carrier sense (15 01): True open, False
            # closed, None until read. Read only while `watch_squelch` is on -
            # a packet station's channel access wants it four times a
            # second, and nothing else here does, so nobody else pays for it.
            "busy": None,
            # DATA MOD as the radio last answered it (1A 05 <model item>),
            # by name ("usb", "lan", "mic"...), None until read or on a model
            # with no `data_mod` row. See `set_data_mod`.
            "data_mod": None,
        }
        self._link: Any = None
        #: Who wants audio, and which receiver each of them asked for. The
        #: value is a receiver key or None, and None means "whichever one this
        #: radio calls its own" - which is every caller written before a radio
        #: could have two, and is still every caller today.
        self._audio_listeners: list[tuple[Callable[[bytes], None], str | None]] = []
        self._audio_bytes = 0
        self._scope = civ.ScopeAssembler()
        self._scope_listeners: list = []
        self._spectrum: dict | None = None
        self._last_wave = 0.0
        #: Whether this module has switched the radio's CI-V scope output on
        #: and not yet off again, and when the last scope listener left. The
        #: output is ours to switch off only if we switched it on.
        self._scope_output = False
        self._scope_idle_since: float | None = None
        self._tasks: list[asyncio.Task] = []
        self._listeners: list[Callable[[dict], None]] = []
        self._buf = b""
        #: When the radio last said anything in CI-V, and when the link last
        #: came up; `asleep` is a quiet stretch measured from the later one.
        self._last_frame = 0.0
        self._connected_at = 0.0
        # One outstanding question per command, so a reply can wake whoever
        # asked. Keyed by the command bytes that identify the answer.
        #: Futures waiting for an answer, by the answer's first one or two
        #: bytes, each with the test the answer must pass (None: any).
        self._waiters: dict[bytes, list[tuple[asyncio.Future, Callable[[bytes], bool] | None]]] = {}
        self._last_tx = 0.0
        #: The lease this rig was keyed under, asked again on every poll; and
        #: an unkey that could not be sent (no link) and is still owed. See
        #: `set_ptt` and `_watch_lease`.
        self._lease: TxLease | None = None
        self._unkey_owed = False
        #: Set whenever the operator changes their mind, so the supervisor
        #: stops waiting and acts. Without it a Connect pressed during the
        #: thirty-second backoff would do nothing visible for half a minute,
        #: which reads as a button that does not work.
        self._wake = asyncio.Event()
        #: External P.AMP menu items still to be read on this connection, and
        #: when they were last asked for. Read per connection (and again now
        #: and then, see _poll) rather than every second: the operator changes
        #: it in a menu, not with a key.
        self._menu_pending: set[int] = self._menu_items()
        self._menu_asked = 0.0
        #: Whether the DV status said a voice call was being received at its
        #: last answer: a rise is a new over.
        self._dv_voice = False
        #: Whether the poll reads the squelch (15 01) - `watch_squelch`.
        self._watch_squelch = False
        #: DATA MOD as found before `set_data_mod` first changed it, for
        #: `restore_data_mod`: leave the radio as it was found.
        self._data_mod_saved: int | None = None

    @property
    def wanted(self) -> bool:
        """Whether a link to this radio is wanted. Kept in `state`, not beside it.

        One place of record: the supervisor reads it to decide whether to
        connect, and the page reads it out of the same dictionary as
        everything else, so the two cannot disagree.
        """
        return bool(self.state["wanted"])

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._supervise()),
            asyncio.create_task(self._poll()),
            asyncio.create_task(self._keep_scope()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        await self._teardown()

    async def connect(self) -> None:
        """The operator wants this radio. Safe to press at any time.

        It asks rather than connects, and that is the whole of why there is
        no double connection to worry about: the supervisor is the only thing
        in this class that ever builds a link, so pressing this while one is
        already up, or while an attempt is in flight, moves a boolean and sets
        an event and nothing else happens. The page then watches `link_state`
        go connecting and then connected, which is the same path a connection
        nobody pressed takes.

        The last failure is cleared here because asking again is asking for a
        fresh answer: leaving "the radio is in use by SHACK-PC" on the screen
        after somebody has freed it and pressed Connect would be the page
        reporting an old fact as a current one.
        """
        self._set(wanted=True, error=None)
        self._wake.set()

    async def disconnect(self) -> None:
        """Put the link down and leave it down until somebody says otherwise.

        Manual intent wins over the supervisor, which is the point. The
        supervisor exists because a radio switched off mid-session is the
        normal case and reconnecting for ever is the right answer to it - but
        that same loop would undo a deliberate disconnect within a second,
        and an operator who wants to hand the rig to WSJT-X's own network
        client, or to somebody else in the shack, has no way to say so.

        The radio itself is kept. Only the link goes. Everything attached to
        this radio - the rigctl binding, the digital-modes session, the state
        the page renders - goes on pointing at the same object, so a
        disconnect is a radio that says it is not there rather than a radio
        that vanishes out of the payload and takes the screen with it.
        """
        # The last reason goes with it. "The radio is in use by SHACK-PC" is
        # an answer to "why is this not connected", and once nobody is asking
        # for it the honest answer is "because you put it down" and nothing
        # else - a row still showing a network error under the word
        # "disconnected" invites somebody to go and fix something that is not
        # broken.
        self._set(wanted=False, error=None)
        await self._teardown()
        self._wake.set()

    def where(self) -> str:
        """Where this radio is reached, as a string for a person.

        Replaces `radio.host`, which was the one place the LAN transport leaked
        above this file. A radio on the end of a cable has no host, and the two
        callers - a log line, and the test for whether anything is configured
        at all - wanted a description and an emptiness check rather than an
        address.
        """
        link = self._link
        if link is not None:
            got = link.where()
            if got:
                return got
        return self._where

    def link_diagnostics(self) -> dict | None:
        """Whatever the link can say about how it found - or did not find - a radio.

        Asked of the radio rather than of the link because the link is rebuilt
        on every reconnection and nothing outside this file holds one. A
        transport with nothing to add answers None, which is the LAN's answer:
        a LAN radio either responds at its address or it does not, and the
        control stream's own error already says which. The USB transport
        answers with the whole probe transcript, because on that transport the
        port, the speed and the CI-V address were all discovered and any one of
        them can be the reason nothing works.
        """
        link = self._link
        if link is None:
            return None
        got = getattr(link, "diagnostics", None)
        return got() if callable(got) else None

    async def _teardown(self) -> None:
        # The link owns every socket, subprocess and file descriptor it made,
        # and taking it down is what releases the radio: the disconnect packet
        # that frees its single client slot on the LAN, the flock and the
        # descriptor on a serial port. Both matter and for the same reason -
        # the next connection is refused, or gets half the bytes, if this is
        # skipped.
        link, self._link = self._link, None
        if link is not None:
            await link.stop()
        # Half a spectrum collected before the link went is not the first half
        # of the next one, and _last_wave back at zero makes the scope keeper
        # re-ask as soon as the connection returns rather than after its timer.
        self._scope.reset()
        self._last_wave = 0.0
        # A new connection starts with the radio in whatever state its own
        # front panel left it; the radio forgets the output setting across a
        # power cycle in any case. Nothing to switch off until we switch it on.
        self._scope_output = False
        self._buf = b""
        self._connected_at = 0.0
        # The next connection asks the menu again: it may be another radio,
        # or the same one after somebody has been in its SET menu. The last
        # answers stay on show meanwhile rather than blinking to unknown.
        self._menu_pending = self._menu_items()
        self._menu_asked = 0.0
        self._set(connected=False, asleep=False, scope=False, audio=False)

    async def _supervise(self) -> None:
        """Keep a connection up, and rebuild it when the radio goes away.

        A radio that is switched off mid-session is the normal case, not the
        exceptional one, so this reconnects for as long as the module runs
        rather than giving up after a count of tries.

        It reconnects for as long as the module runs *and the operator wants
        it to*. `disconnect()` is the one thing that stops it, and it has to
        be, because an operator handing the rig to another program needs the
        handover to last longer than a second. A loop that reconnected anyway
        would be this module quietly taking a radio back off whoever was
        given it - which on the LAN means the other client is refused with
        no explanation on either screen.

        It watches `last_received` and nothing else, which is what lets one
        loop serve both transports: a LAN radio that stopped answering and a
        serial port whose radio has been switched off look identical from here,
        and both want the same rebuild.

        The tick is a quarter of a second rather than a whole one because it is
        also what notices a link *arriving*. The LAN handshake finishes on its
        own callbacks and the USB probe finishes in its own thread, so this is
        where "connected" gets onto the page; a second of that is a second of a
        page saying the radio is not there when it is. Four wakeups a second
        comparing two floats is nothing, including on a Pi.
        """
        # Doubles from the first pause to BACKOFF_MAX_S, and goes back to
        # the start only once a link has actually connected. It used to go
        # back as soon as `_connect` returned, which is before the radio has
        # answered the login, so a radio that kept refusing was asked again
        # every 1.25 s for ever (GM0OTB's IC-705, 19/10/2026).
        backoff = self.BACKOFF_FIRST_S
        # The pause after a wedged CI-V stream, separate from `backoff`
        # because a wedge happens on a connection that *succeeded*, which is
        # exactly when `backoff` is reset - so it would re-login every eleven
        # seconds for as long as the radio stayed wedged. Doubles to a minute
        # and goes back to the start once a connection delivers.
        wedge_pause = self.WEDGE_PAUSE_S
        while True:
            if not self.wanted:
                # Put down on purpose. Waiting on the event rather than
                # polling the flag is what makes "manual intent wins" true
                # rather than nearly true: there is no tick on which this can
                # decide to have another go.
                await self._idle(None)
                continue
            rejected = False
            try:
                await self._connect()
                while self.wanted:
                    await asyncio.sleep(0.25)
                    link = self._link
                    if link is None:
                        break
                    self._follow(link)
                    self._watch_sleep()
                    if link.connected:
                        backoff = self.BACKOFF_FIRST_S
                    if not link.connected and link.error:
                        rejected = link.error == AUTH_REJECTED
                        break
                    if getattr(link, "wedged", False):
                        log.warning("the radio accepted the CI-V stream but has "
                                    "sent nothing on it - logging out, and in "
                                    "again in %.0fs", wedge_pause)
                        self._set(error="the radio is not answering on its CI-V "
                                        "stream - logging in to it again")
                        backoff = wedge_pause
                        wedge_pause = min(wedge_pause * 2, 60.0)
                        break
                    if self._last_frame > self._connected_at > 0:
                        # A frame on this connection: whatever wedged the
                        # last one has cleared.
                        wedge_pause = self.WEDGE_PAUSE_S
                    silent = time.monotonic() - link.last_received
                    if link.connected and silent > 15.0:
                        log.warning("nothing from the radio for %.0fs, reconnecting", silent)
                        self._set(error="the radio stopped answering")
                        break
                    if not link.connected and silent > 15.0:
                        # Nothing came back at all, and nothing was refused,
                        # so the link has no error of its own to report. Said
                        # here because the alternative is a page that reads
                        # "connecting" for ever with no reason on it, which is
                        # the state this whole control exists to give an
                        # answer to.
                        self._set(error=f"no answer from "
                                        f"{self.where() or 'the radio'} - "
                                        f"check it is switched on and on the "
                                        f"network")
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("connection")
                self._set(error=str(e))
            await self._teardown()
            if not self.wanted:
                continue
            if rejected:
                # A refused login is not tried again until somebody asks:
                # `connect()` (the app's Reconnect, or a new rig built from
                # changed settings). Asking again with the same password only
                # gets the same answer, and a radio counting failed logins
                # may lock the account. The error stays on the page.
                log.error("the radio refused the login; not trying again until asked")
                await self._idle(None)
                continue
            await self._idle(backoff)
            backoff = min(backoff * 2, self.BACKOFF_MAX_S)

    #: The supervisor's pauses between attempts after a failure other than a
    #: refused login (see `_supervise`): 2, 4, 8 ... 60 s. A radio switched
    #: on again is found within a minute; one that is not there is asked
    #: once a minute rather than four times a second.
    BACKOFF_FIRST_S = 2.0
    BACKOFF_MAX_S = 60.0

    #: The first pause between logging out of a wedged CI-V stream and logging
    #: in again (see `_supervise`). Long enough for the radio to have taken
    #: the logout in, short enough that the page is back in seconds.
    WEDGE_PAUSE_S = 3.0

    #: How long a connected radio may say nothing before it is called asleep.
    #: The poll asks three questions every quarter second, so a radio that is
    #: awake answers many times over in this; long enough that a slow first
    #: answer after login is not reported as standby for a moment.
    ASLEEP_AFTER_S = 3.0

    def _watch_sleep(self) -> None:
        """Set `asleep` when the link is up and the radio has gone quiet."""
        if not self.state["connected"]:
            self._connected_at = 0.0
            if self.state["asleep"]:
                self._set(asleep=False)
            return
        now = time.monotonic()
        if not self._connected_at:
            self._connected_at = now
        quiet = now - max(self._last_frame, self._connected_at)
        asleep = quiet > self.ASLEEP_AFTER_S
        if asleep != self.state["asleep"]:
            self._set(asleep=asleep)

    async def _idle(self, timeout: float | None) -> None:
        """Wait out the backoff, or until the operator changes their mind.

        `None` means wait for the operator and nothing else, which is what a
        deliberate disconnect leaves this loop doing.
        """
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _follow(self, link) -> None:
        """Copy what the link has learned into the state everything reads.

        The CI-V address is the interesting one. On the LAN it comes out of the
        capabilities packet; on USB it comes out of the probe, which sweeps the
        addresses because the IC-7610 will not answer a broadcast - measured,
        three times, on a link that answered the same question at 0x98 every
        time. Either way what the radio answered *from* beats what was
        configured, because a radio whose address was changed in its own menu
        is precisely the case this exists to survive.
        """
        reported = link.reported_civ_address
        if reported and reported != self.radio_addr:
            log.info("radio reports CI-V address 0x%02x, using it", reported)
            self.radio_addr = reported
        err = link.poll_error()
        if link.connected:
            self._set(connected=True, radio=link.radio_name, error=None,
                      audio=bool(link.audio_rate),
                      audio_rate=link.audio_rate or None)
        elif err:
            self._set(connected=False, error=err)
        else:
            # Whatever the last attempt said is left standing. Clearing it
            # every quarter second - which is what setting it to the link's
            # None did - meant the reason a radio was not there existed for
            # one tick and then blinked out, so the page could show "failed"
            # with nothing after the colon. It is cleared when the radio comes
            # up, and when somebody presses Connect.
            self._set(connected=False)

    async def _connect(self) -> None:
        """Build a link and start it. Everything transport-shaped is in there.

        The two callbacks are attached before the link is started rather than
        after, because on a serial port the first frame can arrive during
        `start()` - the probe has already had an answer out of the radio by the
        time it hands the port over - and a frame delivered to a callback that
        has not been set yet is one the page never sees.
        """
        link = self._make_link()
        link.on_civ = self._on_civ_data
        # One callback, not two. `on_audio_from` names the receiver every
        # block came from and a link that has one receiver says so, so
        # subscribing to "the audio" and subscribing to a named receiver are
        # the same path with a different filter - which is what stops the two
        # drifting apart into a stream that is counted twice or not at all.
        link.on_audio_from = self._on_audio
        self._link = link
        log.info("connecting to %s", link.where() or self._where or "nowhere")
        await link.start()
        if not self.wanted:
            # Disconnected while the handshake was in flight, which is a few
            # milliseconds on a cable and several round trips on the LAN - so
            # it is reachable by a person, not only in theory. The teardown
            # that ran then took down a link that had not opened a socket yet;
            # by now it has, and nothing else holds this one, so it would go
            # on sending are-you-there to a radio we have just told we were
            # leaving. Stopped by name rather than through `self._link`,
            # which the teardown has already emptied.
            await link.stop()
            self._link = None
            return
        self._follow(link)

    def _on_audio(self, rx: str, pcm: bytes) -> None:
        """One block of PCM from one of this radio's receivers.

        Handed to listeners rather than buffered here. What wants it - a
        decoder, a level meter, a recorder - decides how much to keep, and
        this class holding a buffer nobody reads is how a module that runs for
        a month ends up holding a month of audio.

        The byte total counts every receiver, because it answers "is this radio
        delivering audio at all" and a 7610 sending Main and Sub is delivering
        twice as much of it.
        """
        self._audio_bytes += len(pcm)
        primary = self.primary_receiver()
        for fn, want in list(self._audio_listeners):
            if want is not None and want != rx:
                continue
            if want is None and primary and rx != primary:
                continue
            try:
                fn(pcm)
            except Exception:
                log.exception("audio listener")

    def receivers(self) -> tuple:
        """What this radio is currently delivering audio from.

        One on the LAN, one or two on a cable, none before there is a link -
        and the count is measured rather than assumed, because whether a 7610
        is sending Main and Sub or only one of them is a setting on the radio's
        own front panel that cannot be read over CI-V. `usbaudio.py` decides it
        by looking at the samples.

        Here rather than in `main.py` because it is a fact about a radio, and
        because this is the class everything above the transport reads. It is a
        new member of the `Rig` surface in `link.py` and the first one added
        since that list was written down.
        """
        link = self._link
        got = getattr(link, "receivers", None)
        return got() if callable(got) else ()

    def primary_receiver(self) -> str:
        """Which receiver a caller that did not ask for one gets.

        The first, and the point of naming it is that "the audio" has to go on
        meaning something now that a radio can have two of them. Everything
        written before this - `DecodeRunner` above all - subscribes without
        asking, and must go on hearing exactly what it heard before.
        """
        got = self.receivers()
        return got[0].key if got else ""

    def listen_audio(self, fn, receiver: str | None = None):
        """Subscribe to the receive audio. Returns a function that unsubscribes.

        `receiver` names one of `receivers()`; None means the primary one,
        which is what every existing caller asks for by not asking. The
        keyword is the whole of what a second decoder on the same radio would
        need from this class - `DecodeRunner` takes a radio and calls
        `listen_audio(cb)`, so decoding the Sub is that object wrapped in
        something that passes `receiver="right"` and forwards the rest.

        `fn` is called on this rig's own loop (or, on a cable through
        PortAudio, the capture's thread), the loop every CI-V answer, key
        and unkey also waits on: it must only take the block and return. A
        listener that demodulates there, or takes a lock someone holds while
        calling into this rig, delays or deadlocks the radio - the packet
        link's did both (shack_packet radios/base.py, 18/10/2026). Queue
        the audio and work on it in a thread of your own.
        """
        entry = (fn, receiver)
        self._audio_listeners.append(entry)
        return lambda: (self._audio_listeners.remove(entry)
                        if entry in self._audio_listeners else None)

    def audio_stats(self) -> dict:
        """What the link says about the audio, plus what this class counts.

        The byte total and the listener count stay here because they are facts
        about this object rather than about the transport - a link that is
        rebuilt on every reconnection would reset the total, and a page showing
        a counter that goes back to zero every time a radio blinks is a page
        that cannot be used to tell whether audio is flowing.
        """
        link = self._link
        got = link.audio_stats() if link is not None else {
            "enabled": False, "rate": None, "running": False,
            "packets": 0, "dropped": 0,
        }
        got["bytes"] = self._audio_bytes
        got["listeners"] = len(self._audio_listeners)
        # What is on the cable, for a page that has to decide whether to offer
        # a second decoder. Set here rather than left to each transport so
        # that the answer has one shape whichever link produced it.
        got.setdefault("receivers", [r.public() for r in self.receivers()])
        got["primary"] = self.primary_receiver()
        return got

    # -- the spectrum scope ------------------------------------------------

    #: How long the scope output stays on after the last listener leaves. A
    #: page reload or a TV changing screens closes the scope socket and opens
    #: another a second later, and switching the radio's output off and on
    #: again for that is two settings changes nobody asked for.
    SCOPE_RELEASE_S = 5.0

    async def _keep_scope(self) -> None:
        """Ask for wave data while somebody is watching it, and only then.

        Two commands, not one. 0x27 0x10 switches the scope on and 0x27 0x11
        switches its *output over CI-V* on, and only the first of those is
        needed for the radio to draw a scope on its own screen. So a module
        that sends one and not the other sees a radio that is plainly running a
        spectrum analyser and plainly sending nothing, which reads like a
        broken link rather than a setting.

        Neither setting is ours to keep: the operator can switch the scope off
        from the front panel, and the radio forgets the data output setting
        across a power cycle. So while somebody is listening this re-sends
        both whenever three seconds have passed without a wave-data frame, on
        the same reasoning as the CI-V stream's own watchdog - the cost of
        asking again is two frames, and the cost of not asking is a waterfall
        that never starts.

        **Only while somebody is listening.** This used to ask from the moment
        the radio connected for as long as it stayed connected, with nobody
        watching for most of that: ten sweeps a second of 475 points sharing
        the CI-V stream with the poll loop and every rigctl client, to be
        thrown away. A remote app's scope screen is open for minutes at a
        time, and the page's only while it is on screen, so now the output is
        switched on when the first scope listener arrives and back off
        SCOPE_RELEASE_S after the last one leaves. Only the *output* is
        switched off: whether the radio draws a scope on its own front panel
        is the operator's business, and this never turns that off.
        """
        while True:
            try:
                await asyncio.sleep(1.0)
                if not self.scope or not self.state["connected"]:
                    continue
                if not self._scope_fits():
                    continue
                now = time.monotonic()
                if not self._scope_listeners:
                    if self._scope_idle_since is None:
                        self._scope_idle_since = now
                    if (self._scope_output
                            and now - self._scope_idle_since >= self.SCOPE_RELEASE_S):
                        self._release_scope()
                    continue
                self._scope_idle_since = None
                if now - self._last_wave < 3.0:
                    continue
                self.send(civ.scope_on(True))
                self.send(civ.scope_data_output(True))
                self._scope_output = True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scope")

    def _release_scope(self) -> None:
        """Nobody is watching: stop the radio sending its spectrum over CI-V."""
        if self.send(civ.scope_data_output(False)):
            log.info("scope: nobody is watching, so the radio's CI-V scope "
                     "output is off again")
        self._scope_output = False
        self._scope.reset()
        self._set(scope=False)

    def _scope_fits(self) -> bool:
        """Whether this link can carry a spectrum at all.

        A link is allowed to refuse. A sweep is 475 levels in eleven frames
        about ten times a second, and a serial line at 19200 carries less than
        a third of that - so asking for one does not produce a slow waterfall,
        it produces a link with no room left for the poll loop and a frequency
        display that stops following the dial. Refused with a sentence rather
        than attempted, because "the waterfall is slow" is otherwise diagnosed
        as a bug for a week.
        """
        link = self._link
        if link is None:
            return True
        return bool(getattr(link, "allows_scope", True))

    def _on_scope(self, p: bytes) -> None:
        """One wave-data frame. Listeners hear only whole spectra."""
        self._last_wave = time.monotonic()
        try:
            spectrum = self._scope.add(p)
        except civ.CivError as e:
            log.debug("wave data %s: %s", p[:8].hex(" "), e)
            return
        if spectrum is None:
            return
        self._spectrum = spectrum
        self._set(scope=True, scope_points=spectrum["points"])
        for fn in list(self._scope_listeners):
            try:
                fn(spectrum)
            except Exception:
                log.exception("scope listener")

    def listen_scope(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        """Subscribe to the spectrum. Returns a function that unsubscribes.

        The callback is handed one assembled spectrum at a time, as a dict of
        receiver, mode, start and end frequency in Hz, out_of_range, max (the
        top of the amplitude scale) and data (a list of levels). Same shape as
        listen_audio: this class holds the latest one and nothing more, because
        a scope runs at ten frames a second and a buffer nobody drains is how a
        module that runs for a month ends up holding a month of waterfall.
        """
        self._scope_listeners.append(fn)
        return lambda: (self._scope_listeners.remove(fn)
                        if fn in self._scope_listeners else None)

    def spectrum(self) -> dict | None:
        """The most recent complete spectrum, or None if none has arrived."""
        return self._spectrum

    def scope_stats(self) -> dict:
        s = self._spectrum
        return {
            "enabled": bool(self.scope),
            "running": bool(s) and time.monotonic() - self._last_wave < 3.0,
            "spectra": self._scope.spectra,
            "dropped": self._scope.dropped,
            "points": s["points"] if s else 0,
            "start": s["start"] if s else None,
            "end": s["end"] if s else None,
            "mode": s["mode"] if s else None,
            "max": civ.SCOPE_AMP_MAX,
            "listeners": len(self._scope_listeners),
            "age": round(time.monotonic() - self._last_wave, 2) if s else None,
            # Why there is no waterfall, when the reason is the link rather
            # than the setting. None on a link that can carry one.
            "refused": (None if self._link is None
                        else getattr(self._link, "scope_refusal", lambda: None)()),
        }

    def set_scope(self, on: bool) -> bool:
        """Switch the scope and its CI-V output on or off."""
        if not self.send(civ.scope_on(on)):
            return False
        self.send(civ.scope_data_output(on))
        self.scope = on
        if not on:
            self._scope.reset()
            self._set(scope=False)
        return True

    # -- CI-V in and out ---------------------------------------------------

    def _on_civ_data(self, data: bytes) -> None:
        self._buf += data
        frames, self._buf = civ.split_frames(self._buf)
        for f in frames:
            try:
                to, frm, payload = civ.parse(f)
            except civ.CivError as e:
                log.debug("%s", e)
                continue
            # Our own frames come back to us on this stream. They are addressed
            # to the radio, so the address is what tells an echo from an answer.
            if frm != self.radio_addr:
                continue
            if to not in (self.ctrl_addr, 0x00):
                continue
            # A bare NG is not a radio that is awake. Measured on an IC-9700
            # in standby over the LAN (10/10/2026): it answers every poll -
            # 03, 26 00, 1C 00, 15 02, 0F - with FE FE E0 A2 FA FD and nothing
            # else, so counting any frame as life read a sleeping radio as
            # awake. An awake radio answers the poll's reads with data many
            # times a second, so ignoring NG costs nothing there.
            if payload != bytes([civ.NG]):
                self._last_frame = time.monotonic()
                if self.state["asleep"]:
                    self._set(asleep=False)
            self._on_payload(payload)

    def _on_payload(self, p: bytes) -> None:
        if not p:
            return
        cmd = p[0]
        try:
            if cmd == 0x03 or cmd == 0x00:
                # 0x00 is the unsolicited "the dial moved" frame; same body.
                self._set(freq=civ.decode_freq(p[1:6]))
            elif cmd == 0x26 and len(p) >= 5:
                got = civ.parse_mode_data(p)
                # Only the VFO this module drives. A frame about Main or Sub
                # would otherwise write another VFO's mode into the selected
                # one, which on a page looks exactly like the radio changing
                # mode on its own.
                if got["vfo"] == civ.VFO_SELECTED:
                    filt = got["filter"]
                    self._set(mode=got["mode"], data_mode=got["data"],
                              mode_label=civ.mode_label(got["mode"], got["data"]),
                              filter=civ.FILTERS.get(filt, filt))
            elif cmd == 0x04 or cmd == 0x01:
                mode = civ.MODES.get(p[1])
                filt = p[2] if len(p) > 2 else None
                # 0x01 is the unsolicited "the operator turned the mode knob"
                # frame, and it carries no data flag at all. So the flag stays
                # as last read - except that a mode with no data variant cannot
                # be in data, and carrying it over would put "CW-D" on the page
                # for a radio that has no such thing. Everything else the 0x26
                # poll corrects within a quarter of a second.
                data = bool(self.state.get("data_mode")) and _can_data(self.model, mode)
                self._set(mode=mode, data_mode=data,
                          mode_label=civ.mode_label(mode, data),
                          filter=civ.FILTERS.get(filt, filt))
            elif cmd == 0x1C and len(p) > 2 and p[1] == 0x00:
                self._set(ptt=bool(p[2]))
            elif cmd == 0x0F and len(p) > 1:
                # 01 is split on; 11/12/13 are a duplex shift, not split
                # (civ.DUPLEX_READS).
                self._set(split=p[1] == 0x01)
            elif cmd == 0x27 and len(p) > 1 and p[1] in civ.SCOPE_SETTING_COMMANDS:
                got = civ.parse_scope_setting(p)
                if got is not None:
                    self._set_scope_field(got[1], got[2], receiver=got[0])
            elif cmd == 0x20:
                got = civ.parse_dv(p)
                if got is not None:
                    self._on_dv(*got)
            elif cmd == 0x27 and civ.is_scope_data(p):
                # Wave data, and a great deal of it: 475 levels ten times a
                # second. It goes to its own listeners rather than into the
                # state dict, which is broadcast on every change and would
                # carry a spectrum to every page on every frame.
                self._on_scope(p)
            elif cmd == 0x16 and len(p) > 2 and p[1] == 0x02:
                if self.model is not None and hasattr(self.model, "preamp_from_byte"):
                    level, ext = self.model.preamp_from_byte(p[2])
                else:
                    level, ext = p[2], None
                self._set(preamp=level, preamp_ext=ext, preamp_mode=p[2])
            elif cmd == 0x1A and len(p) >= 5 and p[1] == 0x05:
                self._on_menu(*civ.decode_menu(p))
            elif cmd == 0x11 and len(p) > 1:
                self._set(att=civ.decode_att(p))
            elif cmd == 0x14 and len(p) >= 4 and p[1] == 0x09:
                # The same two BCD bytes, 0000-0255, a meter answers in.
                self._set(cw_pitch=civ.cw_pitch_hz(civ.decode_meter(p[2:4])))
            elif cmd == 0x15 and len(p) > 2 and p[1] == 0x01:
                self._set(busy=civ.decode_squelch(p))
            elif cmd == 0x15 and len(p) > 2:
                raw = civ.decode_meter(p[2:4])
                if p[1] == 0x02:
                    self._set(smeter=raw, smeter_db=round(civ.smeter_db(raw), 1))
                elif p[1] == 0x11:
                    self._set(power=raw)
                elif p[1] == 0x12:
                    self._set(swr=round(civ.swr(raw), 2))
        except civ.CivError as e:
            log.debug("unreadable payload %s: %s", p.hex(" "), e)

        # Wake anything waiting for this particular answer. A waiter with a
        # test stays waiting for an answer that fails it: every 1A 05 answer
        # starts with the same two bytes whatever the menu item, and the
        # poll loop reads menu items of its own.
        for key in (p[:1], p[:2]):
            waiting = self._waiters.get(key)
            if not waiting:
                continue
            keep = []
            for fut, match in waiting:
                if fut.done():
                    continue
                if match is None or match(p):
                    fut.set_result(p)
                else:
                    keep.append((fut, match))
            if keep:
                self._waiters[key] = keep
            else:
                self._waiters.pop(key, None)

    def send(self, command: bytes) -> bool:
        """Send a CI-V command. Returns False if there is no link to send it on.

        The polling loop ignores the answer - a missed reading is replaced a
        quarter of a second later. A command from a person is different: it
        has to be possible to tell that it did not happen, because the only
        other evidence is the radio not changing, and the page was cheerfully
        reporting success either way.
        """
        if self._link is None or not self.state.get("connected"):
            return False
        return self._link.send_civ(
            civ.frame(command, to=self.radio_addr, frm=self.ctrl_addr))

    async def ask(self, command: bytes, key: bytes, timeout: float = 1.0, *,
                  match: Callable[[bytes], bool] | None = None, refused: bool = False) -> bytes | None:
        """Send a command and wait for the frame that answers it.

        `match`, when given, is a test the answer must pass as well as
        starting with `key` (a menu answer must name the item asked for).
        With `refused`, a bare NG (FA) also answers, and is returned as
        b"\xfa": a radio that has no such setting says so at once, where
        otherwise the wait would run to `timeout`. A hint only: an NG says
        nothing about which command it refuses, and the poll's questions
        get NGs too, so a caller confirms it (icom/datasetup.py asks again).
        """
        if self._link is None:
            return None
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        keys = [key] + ([bytes([civ.NG])] if refused else [])
        for k in keys:
            self._waiters.setdefault(k, []).append((fut, match if k == key else None))
        self.send(command)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            for k in keys:
                waiting = self._waiters.get(k)
                if waiting:
                    waiting[:] = [w for w in waiting if w[0] is not fut]
                    if not waiting:
                        self._waiters.pop(k, None)

    # -- polling -----------------------------------------------------------

    async def _poll(self) -> None:
        """Keep the cache current.

        Frequency, mode and PTT every cycle; the meters only while transmitting
        or when something is listening, because an S-meter read is a round trip
        and asking for one ten times a second buys nothing on a quiet band.
        """
        tick = 0
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
                if self._link is None or not self.state["connected"]:
                    continue
                # First, before any reading: a lapsed lease is a transmitter
                # that should already be off, and every other line here can
                # wait a quarter of a second.
                self._watch_lease()
                self.send(civ.read_freq())
                # 0x26 rather than 0x04: it answers the mode, the data flag and
                # the filter to one question, so reading the flag costs nothing
                # extra on a link already carrying a spectrum ten times a
                # second. All three radios in models.py answer it. One that did
                # not would show no mode at all rather than a stale one, which
                # is loud - and the alternative, polling 0x04 as well and
                # merging, is two paths writing one field.
                self.send(civ.read_mode_data())
                self.send(civ.read_ptt())
                if self._watch_squelch:
                    self.send(civ.read_squelch())
                tick += 1
                if self.state["ptt"]:
                    self.send(civ.read_power())
                    self.send(civ.read_swr())
                elif tick % 2 == 0:
                    self.send(civ.read_smeter())
                if tick % 20 == 0:
                    self.send(civ.read_split())
                if tick % 4 == 1:
                    # Once a second: the panel's P.AMP/ATT key sends nothing
                    # unasked, and a band change brings that band's own
                    # settings back, so the only way to stay true is to ask.
                    self._read_front_end()
                    # The CW pitch, once a second in CW only: it is where a
                    # CW decoder listens, the operator can turn it on the
                    # panel unannounced, and nothing else wants it.
                    if self.state.get("mode") in ("CW", "CWR"):
                        self.send(civ.read_cw_pitch())
                    # The scope's settings: once a second while somebody is
                    # watching (the radio's SPAN and REF keys send nothing
                    # unasked), and every five seconds until each has been
                    # answered once, so a client opening the scope sees them
                    # at once. Seven short reads.
                    settings = self.state.get("scope_settings")
                    if settings is not None and (
                            self._scope_listeners
                            or (tick % 20 == 1 and None in settings.values())):
                        self._read_scope_settings()
                # D-STAR: the status four times a second while in DV, which is
                # how a new call is noticed within a poll; the call signs and
                # the message twice a second, and at once when a call starts.
                if self.state.get("mode") == "DV":
                    self.send(civ.read_dv(civ.DV_STATUS))
                    if tick % 2 == 0:
                        self.send(civ.read_dv(civ.DV_CALLSIGNS))
                        self.send(civ.read_dv(civ.DV_MESSAGE))
                if tick % 240 == 0:
                    # About once a minute, the External P.AMP menu again. It
                    # is read per connection, but an operator who has just
                    # switched it on in the radio's SET menu should not have
                    # to reconnect before EXT is offered. Three short reads a
                    # minute is nothing beside the poll.
                    self._menu_pending = self._menu_items()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll")

    # -- state -------------------------------------------------------------

    def _link_word(self) -> str:
        """The one word for what is happening to this link.

        Derived rather than stored, because the three facts it follows from -
        intent, whether a link is up, and whether the last attempt failed -
        are set in six places between the supervisor, the link and the two
        buttons, and a word assigned alongside each of them would be the one
        that went stale. "failed" and "connecting" are the pair worth keeping
        apart: both are not-connected and only one of them has a sentence
        underneath it that somebody can act on.
        """
        if not self.state["wanted"]:
            return "disconnected"
        if self.state["connected"]:
            return "connected"
        if self.state["error"]:
            return "failed"
        return "connecting"

    def _set(self, **kw) -> None:
        changed = False
        for k, v in kw.items():
            if self.state.get(k) != v:
                self.state[k] = v
                changed = True
        word = self._link_word()
        if self.state["link_state"] != word:
            self.state["link_state"] = word
            changed = True
        if changed:
            self.state["updated"] = time.time()
            for fn in list(self._listeners):
                try:
                    fn(self.state)
                except Exception:
                    log.exception("state listener")

    def listen(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    # -- the operations everything above this offers -----------------------

    def set_freq(self, hz: int) -> bool:
        before = self.state.get("freq")
        if not self.send(civ.set_freq(hz)):
            return False
        # Answer from the cache immediately. The radio confirms a moment later
        # and will correct this if it clamped the value to a band edge. Only
        # after the command actually left, though - moving the display for a
        # command that went nowhere is the page telling a lie.
        self._set(freq=hz)
        if before is not None and self._band_of(before) != self._band_of(hz):
            # Another band has its own preamp and attenuator in the radio's
            # memory. Asked now rather than at the next once-a-second read so
            # a P.AMP badge does not sit on 23 cm for a second saying what 2 m
            # had. CI-V is answered in order, so this reads the new band.
            self._read_front_end()
        return True

    def _band_of(self, hz) -> int | None:
        if hz is None or self.model is None:
            return None
        for i, (lo, hi) in enumerate(self.model.rx_ranges):
            if lo <= hz <= hi:
                return i
        return None

    # -- the receive front end -----------------------------------------------

    def _has_front_end(self) -> bool:
        m = self.model
        return m is not None and bool(getattr(m, "preamps", ()) or getattr(m, "attenuators", ()))

    #: How long to wait before asking an unanswered menu read again. A radio
    #: whose firmware answers NG to the item would otherwise be asked three
    #: questions a second for as long as it is connected.
    MENU_RETRY_S = 5.0

    def _read_front_end(self) -> None:
        if not self._has_front_end():
            return
        if self.model.preamps:
            self.send(civ.read_preamp())
        if self.model.attenuators:
            self.send(civ.read_att())
        now = time.monotonic()
        if self._menu_pending and now - self._menu_asked >= self.MENU_RETRY_S:
            self._menu_asked = now
            for item in sorted(self._menu_pending):
                self.send(civ.read_menu(item))

    def _menu_items(self) -> set[int]:
        return {row[3] for row in getattr(self.model, "ext_preamp_menu", ())}

    def _on_menu(self, item: int, value: int) -> None:
        """A 1A 05 answer. Only the External P.AMP items and the scope's
        averaging are kept."""
        table = getattr(self.model, "data_mod", None)
        if table is not None and item == table.item:
            if self._data_mod_saved is None:
                self._data_mod_saved = value
            self._set(data_mod=table.name(value) or f"0x{value:02x}")
            return
        if item == civ.SCOPE_AVERAGING_ITEM and self.state.get("scope_settings") is not None:
            self._set_scope_field("averaging", civ.SCOPE_AVERAGING.get(value))
            return
        for key, _lo, _hi, it in getattr(self.model, "ext_preamp_menu", ()):
            if it == item:
                self._menu_pending.discard(item)
                menu = dict(self.state.get("ext_preamp_menu") or {})
                menu[key] = bool(value)
                self._set(ext_preamp_menu=menu)
                return

    def preamp_mode_refusal(self, mode: int) -> str | None:
        """Why `set_preamp_mode(mode)` would be refused, for a person; None if
        it would be sent.

        The external preamp is only offered where the radio's own SET menu
        says one is fitted on the band in use, and not before the radio has
        said: switching EXT on asks the radio to put DC up the coax to a
        mast-head preamp, and the one setting that says whether anything
        there wants it is that menu. A page asks this before it offers a
        setting, so it can say why rather than show a button that fails.
        """
        m = self.model
        names = m.preamp_mode_names() if m is not None else []
        if not names:
            return _("this radio has no preamp control here")
        try:
            mode = int(mode)
        except (TypeError, ValueError):
            return _("no such preamp setting: {mode}").format(mode=mode)
        if not 0 <= mode < len(names):
            return _("no such preamp setting: {mode}").format(mode=mode)
        if getattr(m, "preamp_mode_is_ext", None) and m.preamp_mode_is_ext(mode):
            row = m.ext_menu_band(self.state.get("freq"))
            if row is None:
                return _("no external preamp on this frequency")
            enabled = (self.state.get("ext_preamp_menu") or {}).get(row[0])
            if enabled is None:
                return _("the radio has not said yet whether an external "
                         "preamp is enabled on {band} MHz").format(band=row[0])
            if not enabled:
                return _("EXT P.AMP is off in the radio's menu for {band} MHz "
                         "(SET > Connectors > External P.AMP)").format(band=row[0])
        return None

    def set_preamp_mode(self, mode: int) -> bool:
        """The preamp control to one whole setting, an index into the model's
        preamp_mode_names(); on an IC-9700 that is the 16 02 byte itself.

        Refused - False, nothing sent - for whatever `preamp_mode_refusal`
        names. Unlike `set_preamp` it needs nothing read first: the setting
        says what the external preamp should be, so there is nothing to carry
        over and nothing to guess. Receive-only, like `set_preamp`.
        """
        if self.preamp_mode_refusal(mode) is not None:
            return False
        mode = int(mode)
        if not self.send(civ.set_preamp(mode)):
            return False
        if hasattr(self.model, "preamp_from_byte"):
            level, ext = self.model.preamp_from_byte(mode)
        else:
            level, ext = mode, None
        self._set(preamp_mode=mode, preamp=level, preamp_ext=ext)
        self._read_front_end()
        return True

    def set_preamp(self, level: int) -> bool:
        """Internal preamp to `level`: 0 off, n the model's preamps[n - 1].

        Refused - False, nothing sent - for a level the model does not list,
        and on an IC-9700 until the radio has said what its external preamp is
        doing. That one byte carries both (CI-V guide p. 5), so a set has to
        send the external preamp's state back unchanged, and guessing it would
        switch a mast-head preamp's supply on or off from a button that says
        nothing about it. The first read lands within a second of connecting.

        Receive-only by construction: it changes what the receiver hears and
        nothing about transmit, so it needs Control and never the lease.
        """
        m = self.model
        if m is None or not m.preamps or not m.has_preamp(int(level)):
            return False
        level = int(level)
        ext = self.state.get("preamp_ext")
        if getattr(m, "preamp_ext", False):
            if ext is None:
                return False
            value = m.preamp_byte(level, ext)
        else:
            value = level
        if not self.send(civ.set_preamp(value)):
            return False
        self._set(preamp=level, preamp_mode=value)
        # Read straight back: the radio may refuse the level on this band, or
        # move the attenuator when the preamp goes on, and the page should say
        # what the radio did rather than what was asked.
        self._read_front_end()
        return True

    def set_att(self, db: int) -> bool:
        """Attenuator to `db`: 0 off, else one of the model's steps."""
        m = self.model
        if m is None or not m.attenuators or not m.has_att(int(db)):
            return False
        db = int(db)
        if not self.send(civ.set_att(db)):
            return False
        self._set(att=db)
        self._read_front_end()
        return True

    # -- the scope's settings ------------------------------------------------

    def _scope_receiver(self) -> int:
        """The scope the settings go to: the one the wave data comes from."""
        s = self._spectrum
        return int(s["receiver"]) & 0x01 if s else 0

    def _read_scope_settings(self) -> None:
        rx = self._scope_receiver()
        for sub in civ.SCOPE_SETTING_COMMANDS:
            self.send(civ.read_scope_setting(sub, rx))
        self.send(civ.read_menu(civ.SCOPE_AVERAGING_ITEM))

    def _set_scope_field(self, key: str, value, receiver: int | None = None) -> None:
        settings = self.state.get("scope_settings")
        if settings is None:
            return
        if receiver is not None and receiver != self._scope_receiver():
            return      # the other receiver's scope, which nothing here shows
        got = dict(settings)
        got[key] = value
        got["receiver"] = self._scope_receiver()
        self._set(scope_settings=got)

    def scope_setting_refusal(self, settings: dict) -> str | None:
        """Why `set_scope_settings(settings)` would be refused, for a person;
        None if every field is one the radio takes. Checked whole before
        anything is sent, so a request is applied entirely or not at all."""
        caps = getattr(self.model, "scope", None)
        if not caps:
            return _("this radio has no scope settings this module can change")
        if not settings:
            return _("nothing to set")
        for key, value in settings.items():
            if key == "span_hz":
                if isinstance(value, bool) or value not in caps["spans_hz"]:
                    return _("span_hz must be one of {spans}").format(
                        spans=", ".join(str(s) for s in caps["spans_hz"]))
            elif key == "mode":
                if value not in caps["modes"]:
                    return _("mode must be one of {modes}").format(modes=", ".join(caps["modes"]))
            elif key == "edge":
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= caps["edges"]:
                    return _("edge must be 1 to {top}").format(top=caps["edges"])
            elif key == "ref_db":
                lim = caps["ref_db"]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or value != value or not lim["min"] <= value <= lim["max"]):
                    return _("ref_db must be a number from {lo} to {hi}").format(
                        lo=lim["min"], hi=lim["max"])
            elif key == "speed":
                if value not in caps["speeds"]:
                    return _("speed must be one of {speeds}").format(speeds=", ".join(caps["speeds"]))
            elif key == "vbw":
                if value not in caps["vbw"]:
                    return _("vbw must be one of {vbws}").format(vbws=", ".join(caps["vbw"]))
            elif key == "averaging":
                if isinstance(value, bool) or value not in caps["averaging"]:
                    return _("averaging must be one of {values}").format(
                        values=", ".join(str(v) for v in caps["averaging"]))
            else:
                return _("no such scope setting: {key}").format(key=key)
        return None

    def set_scope_settings(self, settings: dict) -> bool:
        """Send any of span_hz, mode, edge, ref_db, speed, vbw, averaging.

        Refused - False, nothing sent - for anything `scope_setting_refusal`
        names, and with no link. The cache shows what was asked at once and
        the radio's answers to the read that follows correct it, as the
        preamp does. Receive-only: the scope is a display.
        """
        if self.scope_setting_refusal(settings) is not None:
            return False
        rx = self._scope_receiver()
        frames = []
        for key, value in settings.items():
            if key == "span_hz":
                frames.append(civ.set_scope_span(value // 2, rx))
            elif key == "mode":
                frames.append(civ.set_scope_mode(value, rx))
            elif key == "edge":
                frames.append(civ.set_scope_edge(value, rx))
            elif key == "ref_db":
                frames.append(civ.set_scope_ref(round(float(value) * 2) / 2, rx))
            elif key == "speed":
                frames.append(civ.set_scope_speed(value, rx))
            elif key == "vbw":
                frames.append(civ.set_scope_vbw(value, rx))
            elif key == "averaging":
                frames.append(civ.set_scope_averaging(value))
        for f in frames:
            if not self.send(f):
                return False
        got = dict(self.state["scope_settings"])
        for key, value in settings.items():
            got[key] = round(float(value) * 2) / 2 if key == "ref_db" else value
        got["receiver"] = rx
        self._set(scope_settings=got)
        self._read_scope_settings()
        return True

    # -- D-STAR ---------------------------------------------------------------

    def _on_dv(self, block: str, fields: dict | None) -> None:
        """One 20 0x answer: fold it into `dv`, and notice a new over."""
        if block == "status":
            if fields is None:
                return
            voice = fields["voice"]
            rose = voice and not self._dv_voice
            self._dv_voice = voice
            dv = dict(self.state.get("dv") or DV_BLANK)
            dv["receiving"] = voice
            dv["packet_loss"] = fields["packet_loss"]
            if rose:
                # A call has begun: read who it is now rather than at the
                # next half-second, and date it from here.
                dv["heard_at"] = time.time()
                self.send(civ.read_dv(civ.DV_CALLSIGNS))
                self.send(civ.read_dv(civ.DV_MESSAGE))
            self._set(dv=dv)
            return
        if fields is None:
            return      # nothing heard since the radio was switched on
        dv = dict(self.state.get("dv") or DV_BLANK)
        if block == "callsigns":
            new_call = (fields["caller"], fields["suffix"], fields["ur"]) != (
                dv.get("caller"), dv.get("suffix"), dv.get("ur"))
            for key in ("caller", "suffix", "ur", "rpt1", "rpt2"):
                dv[key] = fields[key]
            if new_call and fields["caller"]:
                # A different station from the last: a new over even if the
                # status read missed its start (a short over between polls).
                dv["heard_at"] = time.time()
        else:
            # The message belongs to the caller it names; one left over from
            # the last station is not shown against this one.
            dv["message"] = fields["message"]
        self._set(dv=dv)

    def set_mode(self, mode: str, filt: int | None = None,
                 data: bool | None = None, vfo: int | None = None) -> bool:
        """Mode, data flag and filter, sent as the single frame 0x26 carries.

        `data` is three-valued on purpose, and the distinction is the one thing
        here that will get quietly reverted if it is not written down. None
        means "leave the flag as the radio has it", which is what an internal
        caller wants: set_filter re-sends the current mode to change the filter
        number, and a default of False there would drop the operator out of
        USB-D every time they touched the filter. A caller that means a
        particular flag says so, and the web API always does - pressing USB in
        the mode menu means USB, not "USB and whatever data happened to be".

        A mode name may also carry the flag: "USB-D" is accepted and means the
        same as ("USB", data=True), so a caller holding the name that is on the
        screen can hand it straight back.
        """
        mode, suffix = civ.split_mode_label(mode)
        if suffix:
            data = True
        # A radio that does not have the mode is told nothing rather than sent
        # a byte it will ignore. Silence is the failure mode CI-V gives for an
        # unsupported mode, and it is indistinguishable from a dead link.
        if self.model is not None and not self.model.has_mode(mode):
            return False
        if data is None:
            data = bool(self.state.get("data_mode"))
        if data and not _can_data(self.model, mode):
            # Same reasoning one line up, for the flag rather than the mode:
            # the radio takes the frame, applies the mode and answers with the
            # flag clear, so the page flicks to USB-D and back a quarter of a
            # second later with nothing said about why.
            return False
        current = self.state.get("filter")
        if filt is None:
            filt = {"FIL1": 1, "FIL2": 2, "FIL3": 3}.get(current, 1)
        if vfo is None:
            vfo = civ.VFO_SELECTED
        if not self.send(civ.set_mode_data(mode, data, filt, vfo)):
            return False
        self._set(mode=mode, data_mode=data, mode_label=civ.mode_label(mode, data))
        return True

    def set_data(self, on: bool) -> bool:
        """Turn the data flag on or off, leaving the mode and filter alone.

        Separate from set_mode because the flag is a toggle on the radio's own
        front panel and should be one here too - the alternative is a mode menu
        with every entry twice. It needs a mode already known, because 0x26
        carries all three fields together and there is nothing to send the flag
        with otherwise; that is a refusal rather than a guess at USB.
        """
        mode = self.state.get("mode")
        if not mode:
            return False
        return self.set_mode(mode, data=bool(on))

    # ---- the transmit audio sink -------------------------------------------
    #
    # digi/session.py asks the radio for these; they forward to the audio
    # stream, and answer "no" when there is not one. Keeping them here rather
    # than handing the stream out means nothing above has to know whether the
    # radio is connected to ask whether it can transmit.

    # They forward to the *link* now rather than to the audio stream, which is
    # the same forwarding one layer further down: a link either has a transmit
    # path or does not, and which one it is, is its business. The LAN link has
    # one because the audio stream grew one; the USB link answers no, because
    # stage one of docs/TRANSPORTS.md carries no audio - and stage three is
    # `aplay` on its stdin, which is where this gets its second implementation
    # and the reason the forwarding is worth having.

    @property
    def tx_rate(self) -> int:
        return getattr(self._link, "tx_rate", 0) if self._link else 0

    @property
    def tx_audio_ready(self) -> bool:
        return bool(getattr(self._link, "tx_audio_ready", False)) if self._link else False

    @property
    def tx_drain_s(self) -> float:
        return float(getattr(self._link, "tx_drain_s", 0.0)) if self._link else 0.0

    def send_audio(self, pcm: bytes) -> bool:
        """Transmit audio, refused unless this rig is keyed under a live lease.

        New in the library. The Icom app forwarded audio whenever the link had
        a transmit path, on the reasoning that audio does not key a radio -
        which is true until the radio's VOX is on with its modulation source
        set to LAN or USB, and then audio *is* keying, with no lease anywhere.
        The app's transmitter already keys before its first sample and unkeys
        after its last, so requiring the key costs it nothing.
        """
        if not self._link or not self._keyed_under_lease():
            return False
        return bool(self._link.send_audio(pcm))

    def _keyed_under_lease(self) -> bool:
        lease = self._lease
        if lease is None:
            return False
        try:
            return lease.refusal() is None
        except Exception:
            log.exception("asking %s's lease", getattr(lease, "holder", "?"))
            return False

    def tx_end(self) -> None:
        if self._link and hasattr(self._link, "tx_end"):
            self._link.tx_end()

    def set_ptt(self, on: bool, lease: TxLease | None = None) -> bool:
        """Key the radio under `lease`, or unkey it. The only way to transmit.

        Keying raises PttRefused - not False - when there is no lease, when the
        lease answers a refusal, when `allow_ptt` is off, or when the radio is
        tuned somewhere this model may not transmit. Those are all decisions,
        and a caller that hits one has either a bug or a sentence to show a
        person; False stays what it has always meant, "the command did not
        reach the radio". The lease is checked first so that "no lease, no
        transmit" holds whatever else is configured.

        Unkeying needs no lease and is never refused here, and the asymmetry is
        the point. Testing permission on both edges looks intentional and was
        the shape that ends with a key stuck down: unkeying is the one command
        that must always be attempted, including - especially - when the
        module believes transmitting is switched off, because that is the
        state it will be in after a restart it did not choose. An unkey that
        cannot be sent for want of a link is remembered and sent as soon as
        the link is back.
        """
        if not on:
            self._lease = None
            if not self.send(civ.set_ptt(False)):
                self._unkey_owed = True
                return False
            self._unkey_owed = False
            self._set(ptt=False)
            return True

        txlease.check(lease)
        if not self.allow_ptt:
            raise PttRefused(_("transmitting is switched off for this rig"))
        freq = self.state.get("freq")
        if freq is None:
            raise PttRefused(_("the rig's frequency is not known yet, so it is not keyed"))
        if self.model is not None and not self.model.can_transmit(freq):
            # A floor, not the band-plan check: the app's lease has already
            # asked shack_bandplan whether this operator may transmit here.
            # This only stops a rig being keyed outside every amateur band
            # its model has, which no lease should ever have allowed.
            raise PttRefused(_("{mhz} MHz is outside the {radio}'s transmit bands ({bands})").format(
                mhz=f"{freq / 1e6:.6f}", radio=self.model.name, bands=self.model.transmit_bands_text()))
        if not self.send(civ.set_ptt(True)):
            return False
        self._lease = lease
        self._set(ptt=True)
        return True

    def _watch_lease(self) -> None:
        """Unkey when the lease this rig was keyed under stops permitting.

        Called from the poll loop. A lease that raises is treated as one that
        refuses: an app whose lease object has broken is not in a position to
        be keeping a transmitter on the air.
        """
        if self._unkey_owed:
            if self.send(civ.set_ptt(False)):
                log.info("sent the unkey that was owed from while the link was down")
                self._unkey_owed = False
                self._set(ptt=False)
        lease = self._lease
        if lease is None:
            return
        try:
            why = lease.refusal()
        except Exception as e:
            log.exception("asking %s's lease", getattr(lease, "holder", "?"))
            why = f"the lease could not be checked: {e}"
        if why is None:
            return
        log.warning("unkeying %s: %s", self.where() or "the rig", why)
        self._lease = None
        if self.send(civ.set_ptt(False)):
            self._set(ptt=False)
        else:
            self._unkey_owed = True

    @property
    def lease_holder(self) -> str | None:
        """Who this rig is keyed for, or None. For a page and for rigctl."""
        lease = self._lease
        return getattr(lease, "holder", None) if lease is not None else None

    def set_power(self, on: bool) -> bool:
        """Switch the radio on (out of standby) or off (into it).

        Sendable while the radio is asleep, which is the point: the link is up
        and the CI-V stream is open even though nothing answers on it, so this
        goes out exactly as any other command does. A power-on carries the
        link's wake preamble - a run of FE on a serial line, nothing on the LAN
        (see civ.set_power).

        Nothing is cached. The evidence that it worked is the radio answering
        the poll again, which clears `asleep`; a power-off is seen as the
        reverse a few seconds later. Neither touches the transmitter: an off
        while keyed takes the transmitter down with everything else, and an on
        wakes a radio receiving, as it always comes up.
        """
        link = self._link
        if link is None or not self.state.get("connected"):
            return False
        frame = civ.frame(civ.set_power(on), to=self.radio_addr, frm=self.ctrl_addr)
        if on:
            preamble = getattr(link, "wake_preamble", None)
            frame = (preamble() if callable(preamble) else b"") + frame
        log.info("power %s sent to %s", "on" if on else "off", self.where() or "the radio")
        return bool(link.send_civ(frame))

    # -- for a packet station (shack_packet.radios) --------------------------

    def watch_squelch(self, on: bool = True) -> None:
        """Read the squelch (15 01) on every poll while on, into
        `state["busy"]`. Off by default; a packet link switches it on."""
        self._watch_squelch = bool(on)
        if not on:
            self._set(busy=None)

    def set_simplex(self) -> bool:
        """Duplex off and every tone function off: 0F 10, 16 42/43/4B 00
        (civ.py says where each comes from). True only if all four went."""
        ok = self.send(civ.set_simplex())
        for name in civ.TONE_FUNCTIONS:
            ok = self.send(civ.set_tone_function(name, False)) and ok
        return ok

    def data_mod_refusal(self, source: str) -> str | None:
        """Why `set_data_mod(source)` would not be sent, for a person; None
        if it would."""
        table = getattr(self.model, "data_mod", None)
        name = self.model.name if self.model is not None else _("this radio")
        if table is None or not table.confirmed:
            return _("this library does not know where the {radio} keeps DATA MOD; set it to {source} "
                     "on the radio (SET > Connectors > MOD Input)").format(radio=name, source=source.upper())
        if table.value(source) is None:
            return _("the {radio}'s DATA MOD has no {source} setting").format(radio=name, source=source.upper())
        if self.state.get("data_mod") is None:
            return _("DATA MOD has not been read yet, so it could not be put back afterwards")
        return None

    def read_data_mod(self) -> bool:
        """Ask for DATA MOD; the answer lands in `state["data_mod"]`."""
        table = getattr(self.model, "data_mod", None)
        if table is None:
            return False
        return self.send(civ.read_menu(table.item))

    def set_data_mod(self, source: str) -> bool:
        """Set DATA MOD to `source` ("usb", "lan"...), the one menu write
        IcomRig makes besides the scope's averaging.

        Refused (False) unless the model's `data_mod` row is confirmed from
        Icom's guide, names `source`, and the setting has been read first:
        the first value read is kept and `restore_data_mod` puts it back, as
        NB's setup assistant does (GUIDE: leave as found).
        """
        if self.data_mod_refusal(source) is not None:
            return False
        table = self.model.data_mod
        if not self.send(civ.set_data_mod(table.item, table.value(source))):
            return False
        self._set(data_mod=source)
        return True

    def restore_data_mod(self) -> bool:
        """Put DATA MOD back as it was first read. True if there was nothing
        to put back or the write went."""
        table = getattr(self.model, "data_mod", None)
        saved = self._data_mod_saved
        if table is None or saved is None or not table.confirmed:
            return True
        if table.name(saved) == self.state.get("data_mod"):
            return True
        if not self.send(civ.set_data_mod(table.item, saved)):
            return False
        self._set(data_mod=table.name(saved))
        return True

    def set_vfo(self, vfo: str) -> bool:
        if not self.send(civ.set_vfo(vfo)):
            return False
        self._set(vfo=vfo)
        return True

    def set_split(self, on: bool) -> bool:
        if not self.send(civ.set_split(on)):
            return False
        self._set(split=on)
        return True


#: The Icom app's name for this class. Kept so its adoption is an import change.
Radio = IcomRig
