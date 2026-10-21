#!/usr/bin/env python3
"""Receive-only IC-9700 LAN check: log in, read the frequency, mode and PTT
state, count receive audio, log out. Writes no setting and never keys
(shack_rig's IcomRig made with tx_enabled=False; no CI-V write is sent).

Used by the two-station rig to prove the tailnet relay (icom_relay.py) from
either end: on shack against shack, and on station A's Windows VM,
where it runs from a copy of shack_rig's source (pure Python, no
dependencies).

    icom_rxcheck.py RADIO_JSON HOST [SECONDS]

RADIO_JSON holds RADIO_USER and RADIO_PASSWORD (0600; never printed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time


async def check(conf: dict, host: str, secs: float, client_name: str = "shack-packet-rxcheck") -> dict:
    from shack_rig.icom import make_rig
    profile = {"model": "ic-9700", "transport": "lan", "host": host, "port": 50001,
               "username": conf["RADIO_USER"], "audio_rate": 12000}
    rig = make_rig(profile, password=conf["RADIO_PASSWORD"], tx_enabled=False, poll_interval=0.25,
                   scope=False, client_name=client_name)
    got: list[int] = []
    rig.listen_audio(lambda pcm: got.append(len(pcm) // 2))
    out: dict = {"host": host}
    t = time.time()
    await rig.start()
    try:
        while not (rig.state.get("connected") and rig.state.get("freq")):
            if time.time() - t > 20:
                out["error"] = str(rig.state.get("error") or "no answer in 20 s")
                return out
            await asyncio.sleep(0.1)
        out["login_s"] = round(time.time() - t, 2)
        out["freq"] = rig.state.get("freq")
        out["mode"] = rig.state.get("mode")
        ptt = await rig.ask(b"\x1c\x00", b"\x1c\x00", 1.0)
        out["ptt"] = ptt.hex(" ") if ptt else None
        got.clear()
        await asyncio.sleep(secs)
        out["rx_audio_s"] = round(sum(got) / 12000, 2)
        out["listened_s"] = secs
    finally:
        await rig.stop()
    return out


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    conf = json.load(open(sys.argv[1]))
    host = sys.argv[2]
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
    out = asyncio.run(check(conf, host, secs))
    print(json.dumps(out))
    ok = "error" not in out and out.get("ptt") == "1c 00 00" and out.get("rx_audio_s", 0) > 0.9 * secs
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
