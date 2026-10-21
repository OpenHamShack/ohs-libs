"""Radio links: the engine's channel.RadioLink, for real radios (step 3).

| Link | Radio |
|---|---|
| `IcomLink` | IC-9700 over LAN or USB, IC-705 over USB or Wi-Fi, through shack_rig's IcomRig |
| `YaesuLink` | FTX-1 over USB, through shack_rig's YaesuRig |
| `KenwoodLink` | TM-V71: PC port control through shack_rig's KenwoodRig, packet through a sound-card interface on its DATA jack |
| `SoundcardLink` | any radio on a sound card: PTT by RTS/DTR, VOX, or none |
| `PlutoLink` | a Pluto (AD936x) through libiio: this link is the FM modem and the TX guard |
| `HackRFLink` | a HackRF One through libhackrf: half-duplex FM, the same modem as PlutoLink's |
| `SdrRxLink` | any receive-only SDR (an sdr.IqSource: RTL-SDR, rtl_tcp, SDRplay): listens, never keys |

All of them are `base.AudioRadioLink`: the station checked at the moment of
keying and every tick while keyed, a hard key-down limit, unkey on any
error, disconnect or close, audio paced into the radio and the unkey held
until it has played, and a `Claim` seam for the broker. `turnaround`
measures a radio's key-up and unkey times, with the operator present and a
dummy load, and says what TXDELAY to use.

Each but PlutoLink, HackRFLink and SdrRxLink needs shack-rig (the `radios` extra;
those two need numpy and their SDR's library only); importing this package without it is fine until a
link is made.
"""

from __future__ import annotations

from .base import REASONS, AudioRadioLink, Claim, LinkRefused, LocalClaim

__all__ = ["REASONS", "AudioRadioLink", "Claim", "HackRFLink", "IcomLink", "KenwoodLink", "LinkRefused", "LocalClaim",
           "PlutoLink",
           "SdrRxLink", "SoundcardLink", "YaesuLink"]


def __getattr__(name):
    # The links import shack_rig, an optional dependency, only when asked for.
    if name == "IcomLink":
        from .icom import IcomLink
        return IcomLink
    if name == "YaesuLink":
        from .yaesu import YaesuLink
        return YaesuLink
    if name == "KenwoodLink":
        from .kenwood import KenwoodLink
        return KenwoodLink
    if name == "SoundcardLink":
        from .soundcard import SoundcardLink
        return SoundcardLink
    if name == "HackRFLink":
        from .hackrf import HackRFLink
        return HackRFLink
    if name == "SdrRxLink":
        from .sdrrx import SdrRxLink
        return SdrRxLink
    if name == "PlutoLink":
        from .pluto import PlutoLink
        return PlutoLink
    raise AttributeError(name)
