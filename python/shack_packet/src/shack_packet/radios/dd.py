"""23 cm DD as a packet link: AX.25 frames as AXUDP datagrams between two
stations' PCs across a pair of IC-9700s in DD mode (Packet Radio 0.3.0,
experimental, off unless chosen).

**What DD is** (Icom's Advanced Manual, section 13): 128 kbit/s on 23 cm,
and the radio is an Ethernet bridge - whatever frames reach its [LAN] port
go to air, and what it hears comes out of that port. So the two PCs talk
plain IP to each other, on one private subnet, as if on one cable. The
radio's transmitter is gated by TX INHIBIT, which only the front-panel
TRANSMIT key toggles: CI-V can read its power-on default (1A 05 0087) but
cannot set it, so the operator is asked to press it and the app says so.

**Why AXUDP.** Everything the app already does - chat, the link test, file
transfer, mail, IP over AX.25 - runs on AX.25 frames, so a link that
carries frames (channel.RadioLink.carries_frames, like a KISS TNC) gives
all of it DD at once with no second protocol stack. AXUDP is the
encapsulation BPQ32, LinBPQ and JNOS already use for AX.25 over IP: one
frame per datagram, its FCS appended (low byte first), port 10093.

**No listening socket** (Brian's rule for the desktop apps). The socket
is bound to this PC's DD-side address only - never 0.0.0.0 - and
`connect()`ed to the one peer, so the kernel delivers nothing from any
other address and nothing here ever answers anybody else. Both stations
do the same, so each one's first datagram opens the flow both ways, as a
hole punch does. There is no accept, no service, and nothing on the home
LAN can reach it. Rejected: one side listening on TCP for the other
(a server in a desktop app), and rendezvous through openhamshack.com's
relay with the data P2P (the relay would add an internet dependency to a
link whose point is that it needs none; the link test's relay is still
how two stations agree to run a test).

**Transmit.** A datagram is a transmission (the radio keys for it), so
`send_frames` refuses unless keyed, and `key()` asks the station - the
callsign and the arming - exactly as an audio link does: a disarmed
station sends nothing over DD. What this link cannot stop is the PC's
other traffic on that interface (ARP, mDNS, a browser) reaching the radio
and going to air: hence the static addresses, the second NIC or the
controller's filtered "Radio port", and the chatter test
(shack-packet docs/DD.md, tools/dd-chatter-test.py).

**The radio's set-up** (shack_rig.icom.satdd.dd_settings, through
shack_rig.datasetup): the channel, DD, RPS off, RF POWER when asked - read,
journalled, read back, put back on close and after a crash. With no
profile the link carries frames only and the radio is left to the
operator (a radio reached over USB by another program, say).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from pathlib import Path

from .. import hdlc
from ..channel import RadioLink
from .base import LinkRefused

log = logging.getLogger(__name__)

AXUDP_PORT = 10093
#: DD's air rate: what the channel's airtime sums use.
DD_BITRATE = 128_000
#: The UK band plan's 150 kHz high-speed data channels on 23 cm (RSGB
#: 2026, shack-packet GUIDE note "the band plan"): DD only on these.
UK_DD_CHANNELS_HZ = (1_299_075_000, 1_299_225_000, 1_299_375_000, 1_299_525_000, 1_299_675_000)
MAX_DATAGRAM = 2048


def encode(frame: bytes) -> bytes:
    return hdlc.with_fcs(frame)


def decode(datagram: bytes) -> bytes | None:
    """The frame, or None for a datagram whose FCS is wrong (or too short)."""
    if len(datagram) < 17 or not hdlc.fcs_ok(datagram):
        return None
    return bytes(datagram[:-2])


def _addr(text: str, default_port: int = AXUDP_PORT) -> tuple[str, int]:
    host, _, port = str(text or "").strip().rpartition(":")
    if not host:
        host, port = port, ""
    return host, int(port) if port else default_port


class RigOnLoop:
    """A shack_rig IcomRig on an asyncio loop of its own, for CI-V only (no
    audio, no transmit stream): the DD link's and the chatter test's."""

    def __init__(self, profile: dict, password: str = "", rig=None):
        self.profile = {**profile, "audio_rate": 0}
        self.password = password
        self.rig = rig
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self, connect_s: float = 20.0) -> None:
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._loop = loop
        self._thread = threading.Thread(target=run, name="dd rig loop", daemon=True)
        self._thread.start()
        ready.wait(5)
        if self.rig is None:
            from shack_rig.icom import make_rig
            self.rig = self.call(lambda: make_rig(self.profile, password=self.password, tx_enabled=False,
                                                  poll_interval=0.25, scope=False))
        self.call(self.rig.start)
        from shack_rig.icom.lan import AUTH_REJECTED
        end = time.monotonic() + connect_s
        while time.monotonic() < end:
            st = self.rig.state
            if st.get("connected") and st.get("freq"):
                return
            if st.get("error") == AUTH_REJECTED:
                break
            time.sleep(0.05)
        why = self.rig.state.get("error") or "the radio did not answer"
        self.stop()
        # The app's own key for a refused login, as IcomLink gives it.
        raise LinkRefused("radio-auth" if why == AUTH_REJECTED else "radio-error", str(why))

    def call(self, fn, *args, timeout: float = 2.0):
        loop = self._loop
        if loop is None:
            raise OSError("the radio's loop is not running")

        async def run():
            got = fn(*args)
            if asyncio.iscoroutine(got):
                got = await got
            return got

        return asyncio.run_coroutine_threadsafe(run(), loop).result(timeout)

    def stop(self) -> None:
        loop, self._loop = self._loop, None
        if loop is None:
            return
        if self.rig is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.rig.stop(), loop).result(10)
            except Exception:                       # noqa: BLE001 - stopping regardless
                log.exception("stopping the DD rig")
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(5)
        loop.close()


class DdLink(RadioLink):
    """`local` is this PC's address on the DD side ("10.99.0.1" or with a
    port), `peer` the other station's. `profile` and `password` reach the
    radio for its set-up (shack_rig make_rig's profile); `frequency` is the
    DD channel. `sock` is a test's ready socket."""

    carries_frames = True
    #: The modem the channel's airtime and TXDELAY sums use. DD itself is
    #: 128 kbit/s, but every frame also carries Ethernet, IP and UDP headers
    #: (42 bytes) and DD's own framing, none of which the channel counts; at
    #: 76 800 it reckons an over 1.7 times the AX.25 bits' own time, which
    #: covers that and keeps the channel from handing the radio frames
    #: faster than it can send them. There is no 128k modem to name.
    baud = 76_800
    mode = "gfsk76800"
    turnaround_s = 0.0

    def __init__(self, *, local: str, peer: str, station=None, profile: dict | None = None, password: str = "",
                 frequency: int = 1_299_525_000, power_level: int | None = None,
                 setup_dir: str | Path | None = None, rig=None, sock: socket.socket | None = None,
                 connect_s: float = 20.0):
        self.local = _addr(local)
        self.peer = _addr(peer)
        self.station = station
        self.profile = dict(profile) if profile else None
        self.password = password
        self.frequency = int(frequency)
        self.power_level = power_level
        self.setup_dir = Path(setup_dir) if setup_dir else None
        self.connect_s = connect_s
        self._given_rig = rig
        self._sock = sock
        self.sink = None
        self.host: RigOnLoop | None = None
        self._setup = None
        self.setup_report: list = []
        self.setup_restored: list = []
        self.setup: dict = {}
        self._open = False
        self._keyed = False
        self._rx: threading.Thread | None = None
        self.stats = {"sent": 0, "received": 0, "bad_fcs": 0, "refused": 0, "send_errors": 0}

    def attach(self, sink) -> None:
        self.sink = sink
        if self.station is None:
            self.station = getattr(sink, "station", None)

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def keyed(self) -> bool:
        return self._keyed

    # -- open and close ---------------------------------------------------------------

    def open(self) -> DdLink:
        if self._open:
            return self
        if self.frequency not in UK_DD_CHANNELS_HZ:
            raise LinkRefused("dd-channel", f"{self.frequency / 1e6:.3f} MHz is not a 23 cm high-speed data "
                                            "channel (1299.075-1299.675 MHz, 150 kHz apart)")
        if not self.local[0] or self.local[0] in ("0.0.0.0", "::"):
            raise LinkRefused("dd-address", "this PC's address on the DD side is not set")
        if self.profile is not None or self._given_rig is not None:
            self._radio_up()
        try:
            if self._sock is None:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind(self.local)
                s.connect(self.peer)
                self._sock = s
            self._sock.settimeout(0.25)
        except OSError as e:
            self._radio_down()
            raise LinkRefused("dd-address", str(e)) from None
        self._open = True
        self._rx = threading.Thread(target=self._receive, name="dd receive", daemon=True)
        self._rx.start()
        log.info("DD link open: %s:%d <-> %s:%d on %.3f MHz", *self.local, *self.peer, self.frequency / 1e6)
        return self

    def _radio_up(self) -> None:
        from shack_rig.datasetup import DataSetup, Journal, journal_name
        from shack_rig.icom.satdd import dd_settings
        self.host = RigOnLoop(self.profile or {}, self.password, rig=self._given_rig)
        self.host.start(self.connect_s)
        journal = None
        if self.setup_dir is not None:
            where = str((self.profile or {}).get("host") or (self.profile or {}).get("civ_device") or "radio")
            journal = Journal(self.setup_dir / journal_name("icom", "ic-9700-dd", where), radio="IC-9700 DD")
        rig = self.host.rig
        setup = DataSetup(dd_settings(rig, self.host.call, frequency=self.frequency, power_level=self.power_level),
                          journal, radio="IC-9700 DD")
        self._setup = setup
        setup.recover()
        self.setup_report = setup.apply()
        status = {ln.key: ln.status for ln in self.setup_report}
        self.setup = {k: s in ("set", "already") for k, s in status.items()}
        needed = [k for k in ("frequency", "mode") if status.get(k) not in ("set", "already")]
        if self.power_level is not None and status.get("rf_power") not in ("set", "already"):
            needed.append("rf_power")
        if needed:
            self._radio_down()
            raise LinkRefused("dd-refused", ", ".join(needed))

    def _radio_down(self) -> None:
        setup, self._setup = self._setup, None
        if setup is not None:
            try:
                self.setup_restored = setup.restore()
            except Exception:                       # noqa: BLE001 - journalled; recovered next open
                log.exception("putting the radio back from DD")
        host, self.host = self.host, None
        if host is not None:
            host.stop()

    def close(self) -> None:
        self._keyed = False
        was_open, self._open = self._open, False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._rx is not None:
            self._rx.join(2)
            self._rx = None
        self._radio_down()
        if was_open:
            log.info("DD link closed")

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def emergency_unkey(self) -> None:
        """Nothing is keyed by CI-V here; stop sending."""
        self._keyed = False

    # -- the channel's calls ---------------------------------------------------------------

    def key(self) -> None:
        if not self._open:
            self.stats["refused"] += 1
            raise LinkRefused("not-open")
        why = self.station.refusal() if self.station is not None else "no-callsign"
        if why is not None:
            self.stats["refused"] += 1
            raise LinkRefused(why)
        self._keyed = True

    def unkey(self) -> None:
        self._keyed = False

    def abort(self) -> None:
        self._keyed = False

    def send_frames(self, frames, txdelay_s: float = 0.0, airtime_s: float = 0.0, modes=None) -> None:
        if not self._keyed:
            raise LinkRefused("not-keyed")
        sock = self._sock
        for f in frames:
            try:
                sock.send(encode(bytes(f)))
                self.stats["sent"] += 1
            except OSError as e:
                self.stats["send_errors"] += 1
                raise LinkRefused("radio-error", str(e)) from None

    def _receive(self) -> None:
        while self._open:
            sock = self._sock
            if sock is None:
                return
            try:
                data = sock.recv(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                # ECONNREFUSED from a peer not up yet arrives here on a
                # connected UDP socket; it is not the end of the link.
                time.sleep(0.25)
                continue
            frame = decode(data)
            if frame is None:
                self.stats["bad_fcs"] += 1
                continue
            self.stats["received"] += 1
            if self.sink is not None:
                try:
                    self.sink.frame_in(frame)
                except Exception:                   # noqa: BLE001 - one frame, not the link
                    log.exception("a DD frame")
