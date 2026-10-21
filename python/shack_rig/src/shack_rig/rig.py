"""The `Rig` surface: what an app, the rigctl server and a decoder may ask of
any radio, whoever made it.

OpenHamShack Icom arrived at this by accident and then wrote it down
(`icom/link.py`, "the `Rig` surface, which is the other seam and the
acceptance test"): everything above its `Radio` class - the web page, rigctl,
the logbook, the digital modes - read only these names, and none of it had
ever formed a CI-V frame. That property is what lets a second maker's backend
stand here without rewriting the apps, so this library makes it the contract
rather than a happy accident. `IcomRig` is the first implementation; a
Yaesu/Kenwood CAT backend is the second, when one is written.

**State is read from a cache, never asked for.** WSJT-X polls frequency several
times a second and a control deck harder still; sending each question down a
CI-V or CAT link would leave no room for answers. A backend runs one poll loop
and everyone reads `rig.state`; `listen(fn)` is told whenever it changes, and
the radio's own unsolicited reports (turning the dial) land in the same cache.

`state` is a plain dict on purpose: the Icom app broadcasts it to pages as is,
and the keys below are the contract. A backend may add keys of its own; it may
not change the meaning of these.

    connected   bool        a link is up and the radio answers
    wanted      bool        the operator wants a link (connect/disconnect)
    link_state  str         connected | connecting | failed | disconnected
    error       str|None    why not connected, in a sentence for a person
    radio       str|None    what the radio calls itself, when it says
    freq        int|None    Hz, of the VFO the backend drives
    mode        str|None    one of shack_rig.model.MODES
    data_mode   bool        the DATA flag (USB-D is USB + data_mode)
    mode_label  str|None    mode_label(mode, data_mode), for a page
    filter      str|None    the radio's filter name (Icom: FIL1/FIL2/FIL3)
    ptt         bool        the radio says it is transmitting (by any route)
    vfo         str         VFOA | VFOB
    split       bool
    smeter      int|None    raw 0-255 (Icom's scale)
    smeter_db   float|None  dB relative to S9
    preamp      int|None    receive preamp level: 0 off, n = model.preamps[n-1]
    preamp_mode int|None    the preamp control as one setting, an index into
                            model.preamp_mode_names() (IC-9700: OFF, P.AMP,
                            EXT, P.AMP+EXT); the level on radios without EXT
    att         int|None    receive attenuator in dB, 0 off
    power       int|None    raw 0-255 while transmitting
    swr         float|None  ratio while transmitting, 99.0 meaning "high"
    audio       bool        receive audio is flowing
    audio_rate  int|None    Hz of the PCM listen_audio delivers
    scope       bool        whole spectra are arriving
    scope_points int|None   points in the last spectrum
    updated     float       time.time() of the last change

**Spectra** (`listen_scope`, `spectrum()`) are dicts of receiver, mode, start
and end in Hz (edges, even when the radio reported centre and half-span),
out_of_range, max (top of the amplitude scale) and data (a list of levels).
A backend whose radio has no scope never calls the listeners and answers
`spectrum()` with None.

**Transmit is lease-only.** `set_ptt(True, lease)` with a `shack_rig.lease`
TxLease is the only way to key, and `send_audio` is refused unless the rig is
keyed under a lease. See lease.py for why, and for what the app provides.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from .lease import TxLease
from .model import RigModel

#: The state keys every backend fills, in the order the docstring lists them.
STATE_KEYS = (
    "connected", "wanted", "link_state", "error", "radio", "freq", "mode",
    "data_mode", "mode_label", "filter", "ptt", "vfo", "split", "smeter",
    "smeter_db", "preamp", "preamp_mode", "att", "power", "swr", "audio",
    "audio_rate", "scope", "scope_points", "updated",
)


@runtime_checkable
class Rig(Protocol):
    """One radio. Methods that change the radio return False when the command
    could not be sent (no link, or the radio has not got that mode/band);
    they never raise for that. Keying without a valid lease raises PttRefused.
    """

    state: dict[str, Any]
    model: RigModel | None
    allow_ptt: bool

    # lifetime of the object, and the operator's intent about the link inside it
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def where(self) -> str: ...
    def link_diagnostics(self) -> dict | None: ...

    # control
    def set_freq(self, hz: int) -> bool: ...
    def set_mode(self, mode: str, filt: int | None = None,
                 data: bool | None = None, vfo: int | None = None) -> bool: ...
    def set_data(self, on: bool) -> bool: ...
    def set_vfo(self, vfo: str) -> bool: ...
    def set_split(self, on: bool) -> bool: ...
    def set_scope(self, on: bool) -> bool: ...
    # the receive front end: levels the model lists, refused (False) otherwise
    def set_preamp(self, level: int) -> bool: ...
    def set_preamp_mode(self, mode: int) -> bool: ...
    def set_att(self, db: int) -> bool: ...
    def set_ptt(self, on: bool, lease: TxLease | None = None) -> bool: ...

    # what the radio sends
    def listen(self, fn: Callable[[dict], None]) -> Callable[[], None]: ...
    def listen_audio(self, fn: Callable[[bytes], None],
                     receiver: str | None = None) -> Callable[[], None]: ...
    def listen_scope(self, fn: Callable[[dict], None]) -> Callable[[], None]: ...
    def spectrum(self) -> dict | None: ...
    def audio_stats(self) -> dict: ...
    def scope_stats(self) -> dict: ...
    def receivers(self) -> tuple: ...
    def primary_receiver(self) -> str: ...

    # transmit audio (refused unless keyed under a lease)
    @property
    def tx_rate(self) -> int: ...
    @property
    def tx_audio_ready(self) -> bool: ...
    @property
    def tx_drain_s(self) -> float: ...
    def send_audio(self, pcm: bytes) -> bool: ...
    def tx_end(self) -> None: ...
