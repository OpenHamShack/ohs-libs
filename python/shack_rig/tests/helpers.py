"""A link made of a list, and the helpers the Icom app's selftest.py used
around it (`_Wire`, `_nowhere`, `_wired`, `_sent`, `_say`), so the ported
checks read the way they did there."""

from __future__ import annotations

from shack_rig.icom import civ
from shack_rig.icom.rig import IcomRig


class Wire:
    """Stands in for a Link, keeping the frames instead of sending them.

    Enough of one to let the set_* methods run the whole way down: without it
    they stop at "no link" and the frame they would have built is never made,
    which is how a command can be tested and still be wrong on the wire.
    """

    audio_rate = 0
    tx_rate = 0
    tx_audio_ready = False
    tx_drain_s = 0.0
    allows_scope = True

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.audio: list[bytes] = []
        self.on_civ = None
        self.on_audio_from = None
        self.connected = True
        self.error = None
        self.reported_civ_address = None
        self.radio_name = None
        self.last_received = 0.0

    def where(self) -> str:
        return "a list in a test"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.connected = False

    def send_civ(self, packet: bytes) -> bool:
        if not self.connected:
            return False
        self.frames.append(bytes(packet))
        return True

    def poll_error(self):
        return self.error

    def audio_stats(self) -> dict:
        return {"enabled": False, "rate": None, "running": False,
                "packets": 0, "dropped": 0}

    def send_audio(self, pcm: bytes) -> bool:
        self.audio.append(pcm)
        return True

    def tx_end(self) -> None:
        pass

    def diagnostics(self):
        return None


def nowhere(model=None, **kw) -> IcomRig:
    """A rig with nothing on the other end."""
    return IcomRig(make_link=Wire, where="192.0.2.1:50001", model=model, **kw)


def wired(model=None, **kw) -> IcomRig:
    """A rig with a pretend link up, so commands actually produce bytes."""
    r = nowhere(model, **kw)
    r._link = Wire()
    r.state["connected"] = True
    return r


def sent(r) -> bytes:
    """The payload of the last frame the rig sent."""
    _to, _frm, payload = civ.parse(r._link.frames[-1])
    return payload


def payloads(r) -> list[bytes]:
    return [civ.parse(f)[2] for f in r._link.frames]


def say(r, payload: bytes) -> None:
    """Feed a frame in as though the radio had said it."""
    r._on_civ_data(civ.frame(payload, to=0xE0, frm=r.radio_addr))


async def until(cond, timeout: float = 5.0, step: float = 0.05) -> bool:
    """Wait for cond() to hold, for at most `timeout` seconds."""
    import asyncio
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    return bool(cond())


class OnFake:
    """An IcomRig logged in to a FakeIcom on 127.0.0.1, through make_rig.

        async with OnFake(tx_enabled=True) as (rig, radio): ...

    The whole real path - LanLink, the three UDP streams, the poll loop -
    with the radio played by shack_rig.icom.fake. Nothing leaves loopback.
    """

    def __init__(self, profile: dict | None = None, **make_kw):
        self.profile = profile or {}
        self.make_kw = make_kw

    async def __aenter__(self):
        from shack_rig.icom.build import make_rig
        from shack_rig.icom.fake import FakeIcom
        self.radio = FakeIcom(username="op", password="secret")
        await self.radio.start()
        profile = {"model": "ic-9700", "host": "127.0.0.1",
                   "port": self.radio.control_port, "username": "op",
                   "audio_rate": 12000, **self.profile}
        kw = {"password": "secret", "poll_interval": 0.1, **self.make_kw}
        self.rig = make_rig(profile, **kw)
        await self.rig.start()
        ok = await until(lambda: self.rig.state["connected"] and self.rig.state["freq"], 10)
        if not ok:
            await self.__aexit__(None, None, None)
            raise AssertionError(f"never connected to the fake radio: {self.rig.state}")
        return self.rig, self.radio

    async def __aexit__(self, *exc):
        await self.rig.stop()
        await self.radio.stop()
